import asyncio
import hashlib
import json
import secrets
from pathlib import Path

import httpx
import pytest

from contribos.agent.engine import EngineEvent
from contribos.agent.fake import FakeEngine
from contribos.config import Settings
from contribos.github import GitHubClient
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


@pytest.mark.parametrize('owned', [False, True], ids=['fork', 'owned'])
def test_publish_preview_confirmation_reconcile_and_workspace_freshness(tmp_path, local_repo, monkeypatch, owned):
    fork = tmp_path / 'fork.git'
    git(tmp_path, 'clone', '--bare', str(local_repo), str(fork))
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv('CONTRIBOS_GITHUB_TOKEN', token)
    writes = []
    pushes = []
    account = {'login': 'Owner' if owned else 'publisher', 'id': 14, 'type': 'User'}
    source_repo = account['login'] + '/repo'
    repo_id = 27
    base_sha = git(fork, 'rev-parse', 'main')
    metadata = {'title': DRAFT['title'], 'body': DRAFT['body'], 'draft': True}
    engine = FakeEngine()

    class LocalTransfer(GitTransfer):
        async def push_new(self, repo, fork_name, branch, commit, credential):
            outcome = await self._push_branch(self._cache(repo), str(fork), branch, commit, credential, allow_file=True)
            pushes.append(outcome)
            return outcome

        async def push_followup(self, repo, fork_name, branch, commit, parent, credential):
            outcome = await self._push_branch(self._cache(repo), str(fork), branch, commit, credential,
                                              parent=parent, allow_file=True)
            pushes.append(outcome)
            return outcome

    def github(request):
        assert request.headers['authorization'] == 'Bearer ' + token and token not in str(request.url)
        path = request.url.path
        if path == '/repos/owner/repo/pulls/7/reviews':
            return httpx.Response(200, json=[{'state': 'CHANGES_REQUESTED', 'body': 'Please fix the count.'}])
        if path == '/repos/owner/repo/pulls/7/comments':
            return httpx.Response(200, json=[{'path': 'README.md', 'line': 1, 'body': 'Handle empty lines. ' + token}])
        if path == '/graphql':
            mutation = json.loads(request.content)
            assert mutation['variables'] == {'id': 'PR_node'} and 'markPullRequestReadyForReview' in mutation['query']
            assert metadata['draft']
            writes.append('ready')
            metadata['draft'] = False
            raise httpx.ReadTimeout('Response lost')
        if path == '/repos/owner/repo/pulls/7':
            if request.method == 'PATCH':
                payload = json.loads(request.content)
                assert payload == {'title': 'New PR title', 'body': 'New PR body'}
                writes.append('update')
                metadata.update(payload)
                raise httpx.ReadTimeout('Response lost')
            branch = writes[0]['head'].split(':')[-1]
            repo = {'full_name': 'owner/repo', 'id': repo_id, 'default_branch': 'main'}
            return httpx.Response(200, json={'number': 7, 'state': 'open', 'node_id': 'PR_node', **metadata,
                'base': {'ref': 'main', 'sha': base_sha, 'repo': repo},
                'head': {'ref': branch, 'sha': git(fork, 'rev-parse', branch),
                         'repo': {**repo, 'id': repo_id if owned else 37, 'full_name': source_repo}}})
        if path == '/user':
            return httpx.Response(200, json=account)
        if owned and path.casefold() == '/repos/owner/repo':
            return httpx.Response(200, json={'id': repo_id, 'owner': account, 'full_name': source_repo,
                'default_branch': 'main', 'permissions': {'push': True}})
        if path.casefold().startswith(f'/repos/{source_repo.casefold()}/rules/branches/contribos/issue-'):
            assert request.method == 'GET'
            return httpx.Response(200, json=[])
        if path.casefold().startswith(f'/repos/{source_repo.casefold()}/branches/contribos/issue-'):
            assert request.method == 'GET'
            if not pushes:
                return httpx.Response(404)
            branch = path.split('/branches/')[1]
            return httpx.Response(200, json={'name': branch, 'protected': False,
                                             'commit': {'sha': git(fork, 'rev-parse', branch)}})
        if path == '/repos/publisher/repo':
            return httpx.Response(200, json={'fork': True, 'owner': account, 'id': 37, 'full_name': 'publisher/repo', 'parent': {'full_name': 'owner/repo'},
                'default_branch': 'main', 'permissions': {'push': True}})
        if path == '/repos/publisher/repo/git/ref/heads/main':
            return httpx.Response(200, json={})
        assert path == '/repos/owner/repo/pulls'
        if request.method == 'POST':
            writes.append(json.loads(request.content))
            raise httpx.ReadTimeout('Response lost after creating the PR')
        assert request.url.params['head'].startswith(account['login'] + ':contribos/issue-')
        branch = request.url.params['head'].split(':')[1]
        return httpx.Response(200, json=[{'number': 7, 'state': 'open', 'draft': True,
            'base': {'ref': 'main', 'repo': {'full_name': 'owner/repo'}},
            'head': {'ref': branch, 'sha': git(fork, 'rev-parse', branch), 'repo': {'full_name': source_repo}}}]
            if writes else [])

    async def issue_loader(_):
        return {'title': 'Test Issue', 'body': 'Please publish the draft now.', 'linked_prs': [],
                'default_branch': 'main', 'state': 'open'}

    def factory(settings, store):
        reply = EngineEvent('message.agent', {'text': '```json\n' + json.dumps(DRAFT) + '\n```'})
        engine.scripts = [[], [reply], [EngineEvent('message.agent', {'text': 'Publish confirmed.'})], [reply]]
        return TaskSupervisor(settings, store, issue_loader=issue_loader, github=GitHubClient(settings, transport=httpx.MockTransport(github)),
            sandbox_factories={'host': lambda: HostSandbox(settings, clone_source=str(local_repo))},
            engine_factory=lambda *_: engine)

    async def check():
        nonlocal repo_id
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

            async def preview(kind='create_pr'):
                assert (await client.post(path + '/preview', headers=headers, json={'kind': kind})).status_code == 202
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
            if owned:
                assert latest['preview']['owned_repository_id'] == repo_id
                account['id'] += 1
                assert (await confirm(latest)).status_code == 202
                await until(lambda: service.view(task_id)['publication']['error_code'] == 'github_account_changed')
                account['id'] -= 1
                repo_id += 1
                assert (await confirm(latest, 'reconcile')).status_code == 202
                await until(lambda: service.view(task_id)['publication']['error_code'] == 'owned_destination_conflict')
                assert not writes and not pushes
                repo_id -= 1
            assert (await confirm(latest, 'reconcile' if owned else 'confirm')).status_code == 202
            await until(lambda: service.view(task_id)['publication']['error_code'] == 'github_publish_request_failed', timeout=10)
            assert (await confirm(latest)).status_code == 409
            changed.write_text('Unconfirmed post-push edits\n')
            assert (await confirm(latest, 'reconcile')).status_code == 202
            await until(lambda: service.view(task_id)['publication']['status'] == 'succeeded', timeout=10)
            assert store.get(task_id)['status'] == 'published'
            assert writes == [{'title': 'Fix word count', 'body': DRAFT['body'], 'base': 'main', 'draft': True,
                                'head': ('' if owned else 'publisher:') + latest['preview']['branch']}]
            assert pushes == ['pushed', 'existing']
            assert git(fork, 'rev-parse', 'main') == base_sha
            assert git(fork, 'show', latest['preview']['branch'] + ':README.md') == 'Second change'
            assert (await client.get(path)).json()['publication']['pr_url'] == 'https://github.com/owner/repo/pull/7'
            message = await client.post(f'/api/tasks/{task_id}/messages', headers=headers,
                json={'client_message_id': 'review-feedback', 'text': '处理 review 意见'})
            assert message.status_code == 202
            await until(lambda: store.turns(task_id)[-1]['status'] == 'completed')
            prompt = engine.prompts[-1][0].text
            assert 'Please fix the count.' in prompt and 'Handle empty lines.' in prompt and token not in prompt

            first_head = latest['preview']['commit_sha']
            branch = latest['preview']['branch']
            followup = await preview('push_followup')
            next_head = followup['preview']['commit_sha']
            assert followup['preview']['parent_sha'] == first_head
            assert '-Second change' in followup['preview']['diff']
            assert git(fork, 'rev-parse', branch) == first_head
            assert (await confirm(followup, auth=bearer)).status_code == 403
            assert (await confirm(followup)).status_code == 202
            await until(lambda: service.view(task_id)['publication']['status'] == 'succeeded')
            assert git(fork, 'rev-parse', branch) == next_head
            assert git(fork, 'rev-parse', next_head + '^') == first_head
            ready = await preview('mark_ready')
            original = await preview('update_pr')
            edit = await client.patch(path + '/preview/' + original['id'], headers=headers,
                                      json={'title': 'New PR title', 'body': 'New PR body'})
            updated = edit.json()['publication']
            assert (await confirm(ready)).status_code == 409 and (await confirm(original)).status_code == 409
            assert metadata['title'] == DRAFT['title'] and len(writes) == 1
            metadata['body'] = 'Maintainer edit after preview'
            assert (await confirm(updated)).status_code == 202
            await until(lambda: service.view(task_id)['publication']['status'] == 'superseded')
            assert service.view(task_id)['publication']['error_code'] == 'pull_request_changed' and len(writes) == 1
            original = await preview('update_pr')
            edit = await client.patch(path + '/preview/' + original['id'], headers=headers,
                                      json={'title': 'New PR title', 'body': 'New PR body'})
            updated = edit.json()['publication']
            for pub in (updated, None):
                pub = pub or await preview('mark_ready')
                assert (await confirm(pub, auth=bearer)).status_code == 403
                assert (await confirm(pub)).status_code == 202
                await until(lambda: service.view(task_id)['publication']['status'] == 'unknown')
                assert (await confirm(pub, 'reconcile')).status_code == 202
                await until(lambda: service.view(task_id)['publication']['status'] == 'succeeded')
            assert writes[1:] == ['update', 'ready'] and metadata['draft'] is False
            pull = store.db.execute('SELECT * FROM pull_requests WHERE task_id=?', (task_id,)).fetchone()
            assert pull['head_sha'] == next_head and not pull['draft']
            assert json.loads(pull['data_json'])['title'] == 'New PR title'
            assert token not in '\n'.join(store.db.iterdump())
            store.db.execute("UPDATE publications SET preview_json=json_remove(preview_json,'$.publisher_id') WHERE id=?",
                             (followup['id'],))
            assert (await client.post(path + '/preview', headers=headers, json={'kind': 'update_pr'})).status_code == 202
            await until(lambda: not service.view(task_id)['preparing'])
            assert service.view(task_id)['error']['error_code'] == 'publication_identity_unavailable'

    asyncio.run(check())
