import asyncio
import json
import os
import secrets
import select
import signal
import socket
import subprocess
import sys
import time
from contextlib import closing

import httpx
import pytest
from fastapi.testclient import TestClient

from contribos.agent.engine import EngineEvent
from contribos.config import Settings
from contribos.db import connect, migrate
from contribos.desktop_mode import STARTUP_ENV, TOKEN_ENV, DesktopMode, DesktopNotifications, login_path
from contribos.security import LocalAuth
from contribos.tasks.events import TaskEvents
from contribos.tasks.issue import IssueRef
from contribos.tasks.store import TaskStore
from contribos.web.app import create_app


def test_shell_endpoints_require_the_shell_token(tmp_path):
    code, token, local = (secrets.token_urlsafe(32) for _ in range(3))
    app = create_app(Settings(home=tmp_path), desktop=DesktopMode(code, token), auth=LocalAuth(local))
    with TestClient(app, base_url='http://127.0.0.1:8765') as client:
        status = '/api/desktop/status'
        stream = '/api/notifications/stream'
        assert client.get(status).status_code == 401
        assert client.get('/desktop/bootstrap', params={'code': code}, follow_redirects=False).status_code == 303
        assert client.get('/desktop/bootstrap', params={'code': code}).status_code == 401
        assert client.get('/api/tasks').status_code == 200
        for path in (status, stream):
            assert client.get(path).status_code == 401
            assert client.get(path, headers={'Authorization': f'Bearer {local}'}).status_code == 401
            assert client.get(path, headers={'Authorization': f'Bearer {code}'}).status_code == 401
        headers = {'Authorization': f'Bearer {token}'}
        assert client.get(status, headers=headers).json() == {'running_tasks': 0, 'pending_requests': 0}
        assert client.post(status, headers=headers).status_code == 405
        assert client.get('/api/tasks', headers=headers).status_code == 401
        assert client.get(status, headers={**headers, 'Host': 'example.com'}).status_code == 400


def test_notification_settings_and_pending_status(tmp_path):
    async def check():
        with closing(connect(tmp_path / 'test.db')) as db:
            migrate(db)
            store = TaskStore(db)
            events = TaskEvents(store, heartbeat=0.02)
            notices = DesktopNotifications(events)
            events.on_event = notices.on_task_event
            stream = notices.stream()
            assert await anext(stream) == ': connected\n\n'
            tid = store.create(IssueRef('owner/repo', 1), 'codex', 'host')['id']
            store.ready(tid, {}, 'main', 'base', 'work')
            turn = store.claim(tid)['id']
            store.set_setting('notifications', {'request_waiting': False, 'publish_result': False})
            for kind, request_id in [('permission.requested', 'p'), ('question.asked', 'q')]:
                events.publish(tid, turn, EngineEvent(kind, {'request_id': request_id}))
            assert notices.status() == {'running_tasks': 1, 'pending_requests': 2}
            events.publish(tid, turn, EngineEvent('question.answered', {'request_id': 'q'}))
            assert notices.status()['pending_requests'] == 1
            notices.publish('publish.failed', tid)
            assert await anext(stream) == ': heartbeat\n\n'
            store.set_setting('notifications', {})
            cases = [
                ('permission.requested', 'request_waiting'), ('question.asked', 'request_waiting'),
                ('turn.completed', 'turn_completed'), ('turn.failed', 'turn_error'),
                ('task.setup_failed', 'turn_error'), ('system.recovery_failed', 'turn_error'),
                ('publish.succeeded', 'publish_result'), ('publish.failed', 'publish_result'),
                ('discovery.daily_ready', 'daily_recommendations'),
            ]
            for kind, category in cases:
                expected = None if kind.startswith('discovery.') else tid
                if expected:
                    events.publish(tid, turn, EngineEvent(kind, {'detail': 'raw agent text'}))
                else:
                    notices.publish(kind)
                frame = await anext(stream)
                assert frame.startswith('event: notification\n')
                data = json.loads(frame.split('data: ', 1)[1])
                assert data['category'] == category and data['task_id'] == expected
                assert 'raw agent text' not in frame
            store.finish(tid, turn, 'failed', retry_at='2099-01-01T00:00:00Z')
            events.publish(tid, turn, EngineEvent('turn.failed'))
            assert await anext(stream) == ': heartbeat\n\n'
            assert notices.status() == {'running_tasks': 0, 'pending_requests': 0}
            await stream.aclose()
            assert not notices.queues

    asyncio.run(check())


def test_login_path_uses_login_shell_and_handles_failure(tmp_path, monkeypatch):
    shell = tmp_path / 'shell'
    shell.write_text('''#!/bin/sh
test "$1" = "-l" && test "$2" = "-c" || exit 2
echo "Profile banner"
export PATH='/opt/custom tools:/usr/bin'
eval "$3"
''')
    shell.chmod(0o755)
    monkeypatch.setenv('SHELL', str(shell))
    assert login_path() == ('/opt/custom tools:/usr/bin', True)
    shell.write_text('#!/bin/sh\nprintf "CONTRIBOS_PATH=/bin::./local\\n"\n')
    assert login_path() == (os.defpath, False)
    shell.write_text('#!/bin/sh\nsleep 30 & wait\n')
    start = time.monotonic()
    assert login_path(timeout=0.05) == (os.defpath, False)
    assert time.monotonic() - start < 2
    assert login_path(str(tmp_path / 'missing')) == (os.defpath, False)


def test_desktop_sigterm_interrupts_an_active_turn(tmp_path):
    with socket.socket() as port_socket:
        try:
            port_socket.bind(('127.0.0.1', 0))
        except PermissionError:
            pytest.skip('This environment blocks loopback listeners')
        port = port_socket.getsockname()[1]
    code, token = (secrets.token_urlsafe(32) for _ in range(2))
    env = {**os.environ, STARTUP_ENV: code, TOKEN_ENV: token, 'SHELL': '/bin/false', 'CONTRIBOS_HOME': str(tmp_path)}
    entrypoint = """
from functools import partial
from contribos.cli import main
from contribos.web import app
from contribos.web.demo import demo_supervisor

app.create_app = partial(app.create_app, supervisor_factory=demo_supervisor)
raise SystemExit(main())
"""
    with subprocess.Popen(
        [sys.executable, '-c', entrypoint, 'serve', '--desktop', '--port', str(port)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) as process:
        try:
            assert select.select([process.stdout], [], [], 10)[0], 'Backend did not announce readiness'
            assert process.stdout.readline().strip() == f'CONTRIBOS_READY port={port}'
            headers = {'Authorization': f'Bearer {token}'}
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=3) as client:
                assert client.get('/api/health').is_success
                assert client.get('/desktop/bootstrap', params={'code': code}).status_code == 303
                task_id = client.get('/api/tasks').json()['tasks'][0]['id']
                end = time.monotonic() + 5
                while time.monotonic() < end:
                    if client.get('/api/desktop/status', headers=headers).json()['running_tasks'] == 1:
                        break
                    time.sleep(0.02)
                else:
                    raise AssertionError('The demo turn did not start')
                with client.stream('GET', '/api/notifications/stream', headers=headers) as stream:
                    assert stream.status_code == 200 and next(stream.iter_lines()) == ': connected'
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=10)
                assert process.returncode == 0
                assert code not in stdout + stderr and token not in stdout + stderr
                with closing(connect(tmp_path / 'contribos.db')) as db:
                    assert TaskStore(db).get(task_id)['agent_state'] == 'idle'
                    assert db.execute('SELECT status FROM turns WHERE task_id=?', (task_id,)).fetchone()[0] == 'interrupted'
        finally:
            if process.poll() is None:
                process.kill()
