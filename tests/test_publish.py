import asyncio
import hashlib
import json
import secrets
from pathlib import Path

import httpx

from contribos.agent.engine import EngineEvent
from contribos.agent.fake import FakeEngine
from contribos.config import Settings
from contribos.publish.git_transfer import GitTransfer, snapshot_workspace, workspace_patch
from contribos.publish.github_writer import GitHubWriter
from contribos.publish.service import Publisher
from contribos.sandbox.host import HostSandbox
from contribos.security import CSRF_COOKIE, CSRF_HEADER
from contribos.tasks.supervisor import TaskSupervisor
from contribos.web.app import create_app
from test_git_transfer import git
from test_host import local_repo
from test_pr_draft import DRAFT
from test_task_routes import ISSUE, ORIGIN
from test_task_supervisor import until


def test_publish_preview_confirmation_reconcile_and_workspace_freshness(tmp_path, local_repo, monkeypatch):
    fork = tmp_path / 'fork.git'
    git(tmp_path, 'clone', '--bare', str(local_repo), str(fork))
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv('CONTRIBOS_GITHUB_TOKEN', token)
    writes = []
    pushes = []

    class LocalTransfer(GitTransfer):
        async def push_new(self, repo, fork_name, branch, commit, credential):
            outcome = await self._push_branch(self._cache(repo), str(fork), branch, commit, credential, allow_file=True)
            pushes.append(outcome)
            return outcome

    def github(request):
        assert request.headers['authorization'] == 'Bearer ' + token and token not in str(request.url)
        path = request.url.path
        if path == '/user':
            return httpx.Response(200, json={'login': 'publisher'})
        if path == '/repos/publisher/repo':
            return httpx.Response(200, json={'fork': True, 'full_name': 'publisher/repo', 'parent': {'full_name': 'owner/repo'},
                'default_branch': 'main', 'permissions': {'push': True}})
        if path == '/repos/publisher/repo/git/ref/heads/main':
            return httpx.Response(200, json={})
        assert path == '/repos/owner/repo/pulls'
        if request.method == 'POST':
            writes.append(json.loads(request.content))
            raise httpx.ReadTimeout('Response lost after creating the PR')
        return httpx.Response(200, json=[{'number': 7, 'state': 'open', 'draft': True, 'base': {'ref': 'main'},
            'head': {'sha': git(fork, 'rev-parse', writes[0]['head'].split(':')[1])}}] if writes else [])

    async def issue_loader(_):
        return {'title': 'Test Issue', 'body': 'Please publish the draft now.', 'linked_prs': [],
                'default_branch': 'main', 'state': 'open'}

    def factory(settings, store):
        reply = EngineEvent('message.agent', {'text': '```json\n' + json.dumps(DRAFT) + '\n```'})
        engine = FakeEngine(scripts=[[], [reply], [EngineEvent('message.agent', {'text': 'Publish confirmed.'})], [reply]])
        return TaskSupervisor(settings, store, issue_loader=issue_loader,
            sandbox_factories={'host': lambda: HostSandbox(settings, clone_source=str(local_repo))},
            engine_factory=lambda *_: engine)

    async def check():
        settings = Settings(home=tmp_path / 'data')
        app = create_app(settings, supervisor_factory=factory)
        bearer = {'Authorization': 'Bearer ' + settings.local_token_path.read_text().strip()}
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN) as client:
            supervisor = app.state.supervisor
            transfer = LocalTransfer(settings, upstream_url=lambda _: str(local_repo))
            writer = GitHubWriter(supervisor.github.credentials, transfer, transport=httpx.MockTransport(github))
            service = app.state.publisher = Publisher(supervisor, writer=writer, transfer=transfer)
            code = app.state.auth.issue_startup_code()
            assert (await client.get('/desktop/bootstrap', params={'code': code})).status_code == 303
            headers = {'Origin': ORIGIN, CSRF_HEADER: client.cookies[CSRF_COOKIE]}
            assert (await client.put('/api/settings', headers=headers, json={'git_author_name': 'Test',
                'git_author_email': 'test@example.invalid', 'git_dco_signoff': True})).status_code == 200
            created = await client.post('/api/tasks', headers=headers,
                json={'issue_url': ISSUE, 'isolation': 'host', 'trust_confirmed': True})
            task_id = created.json()['task_id']
            store = supervisor.store
            await until(lambda: store.get(task_id)['status'] == 'ready' and store.get(task_id)['agent_state'] == 'idle')
            path = f'/api/tasks/{task_id}/publish'
            changed = settings.home / 'workspaces' / task_id / 'repo' / 'README.md'
            changed.write_text('First change\n')
            task, sandbox = await supervisor.workspace(task_id)
            source = await snapshot_workspace(sandbox, task, 'main')
            review_hash = hashlib.sha256((await workspace_patch(sandbox, source)).encode()).hexdigest()
            store.db.execute("""INSERT INTO reviews(id,task_id,agent_profile,diff_hash_before,status,verdict,created_at)
                VALUES ('review-test',?,'cursor',?,'completed','approve','2026-01-01T00:00:00+00:00')""", (task_id, review_hash))

            async def preview():
                assert (await client.post(path + '/preview', headers=headers)).status_code == 202
                await until(lambda: not service.view(task_id)['preparing'])
                view = (await client.get(path)).json()
                assert view['error'] is None, view
                return view['publication']

            initial = await preview()
            assert initial['preview']['checks']['review']['current'] is True
            assert not writes and not pushes
            edit = await client.patch(path + '/preview/' + initial['id'], headers=headers,
                                      json={'title': 'Edited', 'body': 'Under review'})
            edited = edit.json()['publication']
            assert initial['preview_hash'] != edited['preview_hash']

            message = await client.post(f'/api/tasks/{task_id}/messages', headers=headers, json={
                'client_message_id': 'chat-confirmation',
                'text': f"Confirm publication of preview {edited['preview_hash']}.",
            })
            assert message.status_code == 202
            await until(lambda: store.turns(task_id)[-1]['status'] == 'completed')
            assert service.view(task_id)['publication']['status'] == 'previewed'
            assert not writes and not pushes

            async def confirm(pub, action='confirm', auth=headers):
                return await client.post(path + '/' + action, headers=auth, json={'preview_hash': pub['preview_hash']})

            for extra in ({'action': 'merge'}, {'comment': 'Done'}, {'force': True}, {'branch': 'main'}):
                response = await client.post(path + '/confirm', headers=headers,
                                             json={'preview_hash': edited['preview_hash'], **extra})
                assert response.status_code == 422
            assert (await confirm(initial)).status_code == 409
            assert (await confirm(edited, auth=bearer)).status_code == 403
            assert (await confirm(edited, auth={})).status_code == 403
            changed.write_text('Second change\n')
            assert (await confirm(edited)).status_code == 202
            await until(lambda: service.view(task_id)['publication']['status'] == 'superseded')
            assert not writes and not pushes

            latest = await preview()
            assert latest['preview']['checks']['review']['current'] is False
            assert (await confirm(latest)).status_code == 202
            await until(lambda: service.view(task_id)['publication']['status'] == 'unknown', timeout=10)
            assert (await confirm(latest)).status_code == 409
            changed.write_text('Unconfirmed post-push edits\n')
            assert (await confirm(latest, 'reconcile')).status_code == 202
            await until(lambda: service.view(task_id)['publication']['status'] == 'succeeded', timeout=10)
            assert store.get(task_id)['status'] == 'published'
            assert writes == [{'title': 'Fix word count', 'body': DRAFT['body'], 'base': 'main', 'draft': True,
                                'head': 'publisher:' + latest['preview']['branch']}]
            assert pushes == ['pushed', 'existing']
            assert git(fork, 'show', latest['preview']['branch'] + ':README.md') == 'Second change'
            assert (await client.get(path)).json()['publication']['pr_url'] == 'https://github.com/owner/repo/pull/7'
            assert token not in '\n'.join(store.db.iterdump())

    asyncio.run(check())
