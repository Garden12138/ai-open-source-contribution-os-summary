import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from contribos.agent.acp_engine import AcpEngine
from contribos.agent.fake import FakeEngine
from contribos.agent.profiles import AgentStatus
from contribos.config import Settings
from contribos.sandbox.host import HostSandbox
from contribos.tasks.supervisor import TaskSupervisor
from contribos.web.app import create_app
from test_host import SCRIPT, local_repo
from test_task_routes import ISSUE, ORIGIN
from test_task_supervisor import issue_loader, until


@pytest.mark.parametrize('scenario,status', [('review_valid', 'completed'), ('review_malformed', 'failed'),
                                            ('review_mutates', 'invalid'), ('review_permission', 'completed')])
def test_independent_review_uses_new_read_only_acp_session(tmp_path, local_repo, monkeypatch, scenario, status):
    monkeypatch.setattr('contribos.review.service.check_profile', lambda profile, _: AgentStatus(
        profile.id, 'ok', '/tools/reviewer', '1', 'logged_in', '', '', ''))
    implementation = []

    def engine(sandbox, profile):
        if profile.id == 'codex':
            implementation.append(FakeEngine())
            return implementation[-1]
        profile = replace(profile, command=(sys.executable, str(SCRIPT)), host_args=(),
                          environment={'FAKE_ACP_SCENARIO': scenario})
        return AcpEngine(lambda: sandbox.spawn_agent(profile), stop_process=sandbox.stop)

    def factory(settings, store):
        return TaskSupervisor(settings, store, issue_loader=issue_loader,
            sandbox_factories={'host': lambda: HostSandbox(settings, clone_source=str(local_repo))}, engine_factory=engine)

    async def check():
        settings = Settings(home=tmp_path / 'data')
        app = create_app(settings, supervisor_factory=factory)
        auth = {'Authorization': 'Bearer ' + settings.local_token_path.read_text().strip()}
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers=auth) as client:
            store = app.state.supervisor.store
            store.set_setting('review_agent', 'cursor')
            created = await client.post('/api/tasks', json={'issue_url': ISSUE, 'isolation': 'host', 'trust_confirmed': True})
            task_id = created.json()['task_id']
            await until(lambda: store.get(task_id)['status'] == 'ready' and store.get(task_id)['agent_state'] == 'idle')
            original = store.get(task_id)['session_id']
            (Path(settings.home) / 'workspaces' / task_id / 'repo' / 'README.md').write_text('Changed\n')
            path = f'/api/tasks/{task_id}'
            response = await client.post(path + '/review')
            assert response.status_code == 202
            if scenario == 'review_permission':
                supervisor = app.state.supervisor
                prompts = await until(lambda: supervisor.events.pending(task_id))
                request_id = next(iter(prompts))
                assert (await client.post(path + f'/requests/{request_id}/respond', json={'option_id': 'allow'})).status_code == 200
            service = app.state.reviews
            result = await until(lambda: next((item for item in service.history(task_id) if item['status'] != 'running'), None))
            assert result['status'] == status and result['agent_profile'] == 'cursor'
            row = store.db.execute('SELECT * FROM reviews WHERE id=?', (result['id'],)).fetchone()
            assert row['session_id'] and row['session_id'] != original and store.get(task_id)['session_id'] == original
            assert bool(row['diff_hash_before'] == row['diff_hash_after']) == (status != 'invalid')
            if status == 'completed':
                assert result['verdict'] == 'request_changes' and result['findings'][0]['line'] == 1
                apply = await client.post(path + '/messages', json={'text': '按审查意见修改', 'client_message_id': 'fix-review'})
                assert apply.status_code == 202
                await until(lambda: len(implementation) > 1 and implementation[-1].prompts)
                assert 'Cover the change with a test.' in implementation[-1].prompts[-1][-1].text
                assert implementation[-1].session_id == original
            else:
                assert not result['findings']
                if status == 'failed':
                    assert '{broken' in result['summary']
                apply = await client.post(path + '/reviews/' + result['id'] + '/apply')
                assert apply.status_code == 409
            events = (await client.get(path + '/events')).json()['events']
            assert any(event['type'] == f'review.{status}' for event in events)
            assert all('fake-session' not in json.dumps(event) for event in events if event['type'].startswith('review.'))

    asyncio.run(check())
