import asyncio
import io
import json
import os
import pty
import signal
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from contribos.agent.acp_engine import AcpEngine
from contribos.agent.profiles import PROFILES
from contribos.config import Settings
from contribos.db import connect
from contribos.sandbox.host import HostSandbox
from contribos.tasks.chat import Terminal, question_response, run_chat
from test_host import local_repo

SCRIPT = Path(__file__).with_name('fake_acp_agent.py').resolve()


def test_terminal_reader_preserves_shared_tty_output_flags(monkeypatch):
    async def check():
        master, slave = pty.openpty()
        stdin = os.fdopen(os.dup(slave), 'r')
        console = Terminal(io.StringIO())
        try:
            monkeypatch.setattr(sys, 'stdin', stdin)
            os.write(master, b'continue\n')
            assert await asyncio.wait_for(console.readline(), 2) == 'continue'
            assert os.get_blocking(slave)
        finally:
            console.close()
            stdin.close()
            os.close(slave)
            os.close(master)

    asyncio.run(check())


class ScriptConsole(Terminal):
    def __init__(self, lines=(), react=None):
        super().__init__(io.StringIO())
        self.lines = asyncio.Queue()
        for line in lines:
            self.lines.put_nowait(line)
        self.react = react

    async def readline(self):
        return await self.lines.get()

    def write(self, text):
        super().write(text)
        if self.react:
            self.react(self, text)


def snapshot():
    return {'title': 'Offline issue', 'body': 'Implement a small fix', 'comments': [], 'linked_prs': []}


async def loader(ref):
    return snapshot()


def chat_setup(tmp_path, local_repo, monkeypatch, scenario):
    monkeypatch.setenv('FAKE_ACP_SCENARIO', scenario)
    settings = Settings(home=tmp_path / 'data')
    profile = replace(PROFILES['codex'], command=(sys.executable, str(SCRIPT)), host_args=())
    monkeypatch.setitem(PROFILES, 'codex', profile)
    return settings, HostSandbox(settings, clone_source=str(local_repo))


def test_terminal_workflow_persists_merged_events_and_resumes(tmp_path, local_repo, monkeypatch):
    settings, sandbox = chat_setup(tmp_path, local_repo, monkeypatch, 'workflow')
    messages = iter(['Implement', 'Run tests', '/continue', None])
    def react(console, text):
        if '[turn.completed]' in text:
            console.lines.put_nowait(next(messages))
    console = ScriptConsole(react=react)
    async def check():
        assert await run_chat(settings, 'codex', 'https://github.com/owner/repo/issues/1',
            trust_repo=True, console=console, sandbox=sandbox, loader=loader) == 0
        db = connect(settings.database_path)
        row = db.execute('SELECT * FROM tasks').fetchone()
        assert row['status'] == 'ready' and row['agent_state'] == 'idle'
        assert row['session_id'] == 'fake-session'
        assert db.execute('SELECT count(*) FROM turns WHERE status=\'completed\'').fetchone()[0] == 4
        events = db.execute('SELECT type,payload_json FROM task_events').fetchall()
        assert not any(e['type'].endswith('.delta') for e in events)
        assert any(e['type'] == 'message.agent' and json.loads(e['payload_json'])['text'] == 'A plan.' for e in events)
        assert sum(e['type'] == 'diff.updated' for e in events) == 8
        repo = settings.home / 'workspaces' / row['id'] / 'repo'
        assert (repo / 'agent-output.txt').read_text() == 'implemented\n'
        monkeypatch.setenv('FAKE_ACP_SCENARIO', 'echo')
        resumed = ScriptConsole(react=lambda c, t: c.lines.put_nowait(None) if '[turn.completed]' in t else None)
        assert await run_chat(settings, 'codex', None, resume_task=row['id'], console=resumed) == 0
        assert (repo / 'agent-output.txt').exists()
        assert db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
        assert 'loaded' in resumed.output.getvalue()
        db.close()
    asyncio.run(asyncio.wait_for(check(), 15))
    assert '[command.completed]' in console.output.getvalue()


@pytest.mark.parametrize('scenario,trigger,answer,event_type', [
    ('permission', 'Allow this operation?', 'y', 'permission.resolved'),
    ('permission', 'Allow this operation?', 'n', 'permission.resolved'),
    ('question', 'Answer (single value', 'unit', 'question.answered'),
])
def test_terminal_requests(tmp_path, local_repo, monkeypatch, scenario, trigger, answer, event_type):
    settings, sandbox = chat_setup(tmp_path, local_repo, monkeypatch, scenario)
    def react(console, text):
        if trigger in text:
            console.lines.put_nowait(answer)
        elif '[turn.completed]' in text:
            console.lines.put_nowait(None)
    console = ScriptConsole(react=react)
    async def check():
        assert await run_chat(settings, 'codex', 'https://github.com/owner/repo/issues/1', trust_repo=True,
            console=console, sandbox=sandbox, loader=loader) == 0
        db = connect(settings.database_path)
        event = db.execute('SELECT payload_json FROM task_events WHERE type=?', (event_type,)).fetchone()
        assert event is not None
        payload = json.loads(event[0])
        assert not payload['cancelled']
        if scenario == 'permission':
            assert payload['option_id'] == ('allow' if answer == 'y' else 'deny')
        else:
            assert payload['answers'] == {'test': 'unit'}
        db.close()
    asyncio.run(asyncio.wait_for(check(), 10))


def test_sigint_cancels_then_continue_uses_same_session(tmp_path, local_repo, monkeypatch):
    settings, sandbox = chat_setup(tmp_path, local_repo, monkeypatch, 'slow')
    engines = []
    def factory(sandbox, profile, refresh):
        engine = AcpEngine(lambda: sandbox.spawn_agent(profile), refresh_diff=refresh)
        engines.append(engine)
        return engine
    completed = 0
    def react(console, text):
        nonlocal completed
        if '[turn.completed]' in text:
            completed += 1
            console.lines.put_nowait('/continue' if completed == 1 else None)
    console = ScriptConsole(react=react)
    async def check():
        task = asyncio.create_task(run_chat(settings, 'codex', 'https://github.com/owner/repo/issues/1', trust_repo=True,
            console=console, sandbox=sandbox, loader=loader, engine_factory=factory))
        for index in range(2):
            while not engines or not engines[0].normalizer.parts or completed != index:
                await asyncio.sleep(.01)
            os.kill(os.getpid(), signal.SIGINT)
            while completed <= index:
                await asyncio.sleep(.01)
        assert await task == 0
        db = connect(settings.database_path)
        assert [r[0] for r in db.execute('SELECT status FROM turns')] == ['interrupted', 'interrupted']
        assert len(engines) == 1
        db.close()
    asyncio.run(asyncio.wait_for(check(), 10))


def test_trust_rejection_happens_before_snapshot_or_database(tmp_path):
    async def forbidden(ref):
        raise AssertionError('Must not read GitHub')
    settings = Settings(home=tmp_path / 'unused')
    result = asyncio.run(run_chat(settings, 'codex', 'https://github.com/owner/repo/issues/1',
        console=ScriptConsole(['no']), loader=forbidden))
    assert result == 2
    assert not settings.home.exists()


def test_question_validation_and_terminal_control_stripping():
    payload = {'schema': {'properties': {'test': {'type': 'string', 'enum': ['unit', 'all']}}, 'required': ['test']}}
    assert question_response(payload, 'all').answers == {'test': 'all'}
    with pytest.raises(ValueError):
        question_response(payload, 'invalid')
    console = ScriptConsole()
    console.write('\x1b]52;c;clipboard\x07\x1b[31mSafe\x00 text')
    assert console.output.getvalue() == 'Safe text'


def test_real_cli_stdio_with_fake_agent_subprocess(tmp_path, local_repo):
    import subprocess
    launcher = '''
import asyncio, sys
from dataclasses import replace
from contribos.agent.profiles import PROFILES
from contribos.cli import main
from contribos.sandbox.host import HostSandbox
import contribos.tasks.chat as chat
PROFILES['codex'] = replace(PROFILES['codex'], command=(sys.executable, sys.argv[2]), host_args=())
original = chat.run_chat
async def snapshot(ref):
    return {'title': 'CLI fixture', 'body': 'Offline', 'comments': [], 'linked_prs': []}
async def offline(settings, agent, issue_url, **kwargs):
    return await original(settings, agent, issue_url, loader=snapshot,
        sandbox=HostSandbox(settings, clone_source=sys.argv[1]), **kwargs)
chat.run_chat = offline
raise SystemExit(main(['chat', '--agent', 'codex', '--trust-repo', 'https://github.com/owner/repo/issues/1']))
'''
    result = subprocess.run([sys.executable, '-c', launcher, str(local_repo), str(SCRIPT)],
        input='/continue\n/diff\n', capture_output=True, text=True, timeout=10,
        env={**os.environ, 'CONTRIBOS_HOME': str(tmp_path / 'stdio-data'), 'FAKE_ACP_SCENARIO': 'echo'})
    assert result.returncode == 0, result.stderr
    assert result.stdout.count('[turn.completed]') == 2
    assert 'Report feasibility and a concise plan' in result.stdout
    assert '(no changes)' in result.stdout
    assert not result.stderr
