import asyncio
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from contribos.agent.engine import EngineEvent, RequestResponse, TurnResult
from contribos.agent.fake import FakeEngine
from contribos.agent.profiles import PROFILES
from contribos.config import Settings
from contribos.db import connect, migrate
from contribos.sandbox.base import Workspace
from contribos.sandbox.host import HostSandbox
from contribos.sandbox.processes import process_info
from contribos.tasks.instructions import CONTINUE_PROMPT
from contribos.tasks.issue import IssueRef
from contribos.tasks.store import TaskError, TaskStore
from contribos.tasks.supervisor import TaskSupervisor

REF = IssueRef('owner/repo', 1)


class FakeSandbox:
    kind = 'host'

    async def prepare(self, task):
        return Workspace('/fake/repo', '/fake/context', 'main', 'base', 'work')

    async def write_task_context(self, task):
        assert 'TASK.md' in task.instructions

    async def restore(self, task):
        task.repo_dir, task.context_dir = '/fake/repo', '/fake/context'

    async def diff(self):
        return ''

    async def stop(self):
        pass

    async def destroy(self):
        pass


class GatedEngine(FakeEngine):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.release = asyncio.Event()
        self.gate_cancel = asyncio.Event()

    async def prompt(self, sid, blocks):
        text = blocks[-1].text
        self.calls.append(text)
        if text.startswith('hold'):
            release = asyncio.create_task(self.release.wait())
            stop = asyncio.create_task(self.gate_cancel.wait())
            try:
                await asyncio.wait({release, stop}, return_when=asyncio.FIRST_COMPLETED)
                return TurnResult('cancelled' if self.gate_cancel.is_set() else 'end_turn')
            finally:
                for job in (release, stop):
                    job.cancel()
                await asyncio.gather(release, stop, return_exceptions=True)
                self.release.clear()
                self.gate_cancel.clear()
        return await super().prompt(sid, blocks)

    async def cancel(self, sid):
        self.calls.append('cancel')
        self.gate_cancel.set()
        await super().cancel(sid)


async def issue_loader(ref):
    assert ref == REF
    return {'title': 'Test Issue', 'body': 'Body', 'linked_prs': []}


async def until(check, timeout=6):
    async with asyncio.timeout(timeout):
        while not (value := check()):
            await asyncio.sleep(0.01)
        return value


def offline(settings, store, engine, **kwargs):
    return TaskSupervisor(settings, store, issue_loader=issue_loader,
        sandbox_factories={'host': FakeSandbox}, engine_factory=lambda *_: engine, **kwargs)


async def start(supervisor, store):
    tid = supervisor.create(REF, isolation='host', trust_confirmed=True)['id']
    await until(lambda: store.get(tid)['status'] == 'ready' and store.get(tid)['agent_state'] == 'idle')
    return tid


def test_diff_refresh_can_finish_during_an_edit_turn(tmp_path):
    async def check():
        settings = Settings(home=tmp_path)
        with closing(connect(settings.database_path)) as db:
            migrate(db)
            store = TaskStore(db)
            engine = GatedEngine()
            supervisor = offline(settings, store, engine)
            try:
                task_id = await start(supervisor, store)
                supervisor.send(task_id, 'hold-edit')
                await until(lambda: 'hold-edit' in engine.calls)
                engine.queue.put_nowait(EngineEvent('files.changed', {'status': 'completed', 'paths': ['example.md']}))
                runtime = supervisor.runtimes[task_id]
                await until(lambda: runtime.diff_job is not None and runtime.diff_job.done())
                engine.release.set()
                await until(lambda: store.get(task_id)['agent_state'] in {'idle', 'error'})
                assert store.turns(task_id)[-1]['status'] == 'completed'
            finally:
                await supervisor.shutdown()

    asyncio.run(check())


def test_queue_interrupt_continue_and_concurrent_messages(tmp_path):
    async def check():
        settings = Settings(home=tmp_path)
        with closing(connect(settings.database_path)) as db:
            migrate(db)
            store = TaskStore(db)
            engine = GatedEngine()
            supervisor = offline(settings, store, engine)
            try:
                tid = await start(supervisor, store)
                supervisor.send(tid, 'hold-queue', 'gate')
                await until(lambda: 'hold-queue' in engine.calls)
                async def send(text):
                    return supervisor.send(tid, text, text)
                a, b = await asyncio.gather(send('next A'), send('next B'))
                assert a['id'] != b['id']
                assert [turn['status'] for turn in store.turns(tid)] == ['completed', 'running', 'queued', 'queued']
                assert supervisor.send(tid, 'ignore duplicate', 'next A', interrupt=True)['id'] == a['id']
                engine.release.set()
                await until(lambda: 'next B' in engine.calls)
                assert engine.calls[1:] == ['hold-queue', 'next A', 'next B']
                supervisor.send(tid, 'hold-interrupt', 'interrupted')
                await until(lambda: 'hold-interrupt' in engine.calls)
                supervisor.send(tid, 'interrupting message', 'interrupt', interrupt=True)
                await until(lambda: 'interrupting message' in engine.calls)
                assert engine.calls[-3:] == ['hold-interrupt', 'cancel', 'interrupting message']
                await until(lambda: store.get(tid)['agent_state'] == 'idle')
                supervisor.send(tid, 'hold-stop', 'stopped')
                await until(lambda: 'hold-stop' in engine.calls)
                assert supervisor.interrupt(tid)
                await until(lambda: store.get(tid)['agent_state'] == 'idle')
                supervisor.continue_task(tid)
                await until(lambda: store.get(tid)['agent_state'] == 'idle')
                assert engine.calls[-3:] == ['hold-stop', 'cancel', CONTINUE_PROMPT]
                assert [turn['status'] for turn in store.turns(tid)][-4:] == [
                    'interrupted', 'completed', 'interrupted', 'completed']
            finally:
                await supervisor.shutdown()
    asyncio.run(check())


def test_host_permission_and_question_wait_for_user(tmp_path):
    async def check():
        settings = Settings(home=tmp_path)
        with closing(connect(settings.database_path)) as db:
            migrate(db)
            store = TaskStore(db)
            engine = FakeEngine(scripts=[[
                EngineEvent('permission.requested', {'request_id': 'p', 'options': [{'optionId': 'allow'}]}),
                EngineEvent('question.asked', {'request_id': 'q', 'message': 'Which test?'}),
            ]])
            supervisor = offline(settings, store, engine)
            try:
                tid = supervisor.create(REF, isolation='host', trust_confirmed=True)['id']
                await until(lambda: 'p' in supervisor.events.pending(tid))
                assert store.get(tid)['agent_state'] == 'running'
                with pytest.raises(TaskError, match='Choose'):
                    await supervisor.respond(tid, 'p', RequestResponse(option_id='invalid'))
                await supervisor.respond(tid, 'p', RequestResponse(option_id='allow'))
                await until(lambda: 'q' in supervisor.events.pending(tid))
                assert store.get(tid)['agent_state'] == 'running'
                await supervisor.respond(tid, 'q', RequestResponse(answers={'test': 'unit'}))
                await until(lambda: store.get(tid)['agent_state'] == 'idle')
                assert not supervisor.events.pending(tid)
                assert [event.type for event in supervisor.events.history(tid) if event.type.startswith(('permission.', 'question.'))] == [
                    'permission.requested', 'permission.resolved', 'question.asked', 'question.answered']
            finally:
                await supervisor.shutdown()
    asyncio.run(check())


@pytest.mark.parametrize('scenario', ['no_cancel', 'leaves_child'])
def test_forced_cancel_kills_real_child_and_restores_session(tmp_path, monkeypatch, scenario):
    local_repo = tmp_path / 'upstream'
    local_repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(local_repo)], check=True)
    subprocess.run(['git', '-C', str(local_repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.com',
                    'commit', '--allow-empty', '-qm', 'Initial'], check=True)
    script = Path(__file__).with_name('fake_acp_agent.py')
    monkeypatch.setitem(PROFILES, 'codex', replace(PROFILES['codex'],
        command=(sys.executable, str(script)), environment={'FAKE_ACP_SCENARIO': scenario}))

    async def check():
        settings = Settings(home=tmp_path / 'local')
        with closing(connect(settings.database_path)) as db:
            migrate(db)
            store = TaskStore(db)
            supervisor = TaskSupervisor(settings, store, issue_loader=issue_loader, cancel_timeout=0.6,
                sandbox_factories={'host': lambda: HostSandbox(settings, clone_source=str(local_repo))})
            try:
                tid = await start(supervisor, store)
                runtime = supervisor.runtimes[tid]
                old_engine = runtime.engine
                original_sid = store.get(tid)['session_id']
                supervisor.send(tid, 'Run a long test', 'long')
                pidfile = Path(runtime.context.repo_dir) / 'child-pid'
                await until(pidfile.exists)
                pid = int(pidfile.read_text())
                assert process_info(pid)
                assert supervisor.interrupt(tid)
                await until(lambda: (info := process_info(pid)) is None or info.zombie, timeout=5)
                await until(lambda: store.get(tid)['agent_state'] == 'idle')
                await until(lambda: any(event.type == 'system.recovered' for event in supervisor.events.history(tid)))
                assert runtime.engine is not old_engine
                assert store.get(tid)['session_id'] == original_sid
                assert 'system.cancel_forced' in [event.type for event in supervisor.events.history(tid)]
                supervisor.continue_task(tid)
                await until(lambda: store.get(tid)['agent_state'] == 'idle')
                assert [turn['status'] for turn in store.turns(tid)] == ['completed', 'interrupted', 'completed']
            finally:
                await supervisor.shutdown()
    asyncio.run(check())


@pytest.mark.parametrize(('message', 'code', 'retries'), [
    ('Network connection reset (503)', 'agent_unavailable', [15, 60, 180]),
    ('Login required (401), network request failed', 'authentication_required', []),
    ('Quota exceeded, HTTP 429', 'quota_exhausted', []),
    ('[REDACTED] API: User location is not supported for this service (503)',
     'agent_region_unavailable', []),
])
def test_retry_policy_with_replaced_clock(tmp_path, message, code, retries):
    from datetime import datetime, timedelta, timezone

    class ErrorEngine(FakeEngine):
        async def prompt(self, sid, blocks):
            self.prompts.append(blocks)
            raise PROFILES['antigravity'].classify_error(RuntimeError(message))

    async def check():
        settings = Settings(home=tmp_path)
        now = datetime.now(timezone.utc)
        waits = []
        async def sleep(delay):
            nonlocal now
            waits.append(delay)
            now += timedelta(seconds=delay)
        with closing(connect(settings.database_path)) as db:
            migrate(db)
            store = TaskStore(db)
            engine = ErrorEngine()
            supervisor = offline(settings, store, engine, sleep=sleep, clock=lambda: now)
            try:
                tid = supervisor.create(REF, isolation='host', trust_confirmed=True)['id']
                await until(lambda: store.get(tid)['agent_state'] == 'error')
                assert store.get(tid)['last_error_code'] == code
                assert waits == retries
                turns = store.turns(tid)
                assert [turn['retry_count'] for turn in turns] == list(range(len(retries) + 1))
                assert len(engine.prompts) == len(turns)
                assert all(blocks[0].text == CONTINUE_PROMPT for blocks in engine.prompts[1:])
                scheduled = [e.payload['delay_seconds'] for e in supervisor.events.history(tid)
                             if e.type == 'system.retry_scheduled']
                assert scheduled == retries
                if code == 'agent_region_unavailable':
                    assert 'network egress or proxy' in store.get(tid)['last_error_message']
            finally:
                await supervisor.shutdown()
    asyncio.run(check())


@pytest.mark.parametrize('reason', ['max_tokens', 'max_turn_requests'])
def test_truncated_round_automatically_continues_once(tmp_path, reason):
    class TruncatedEngine(FakeEngine):
        async def prompt(self, sid, blocks):
            self.prompts.append(blocks)
            return TurnResult(reason)

    async def check():
        settings = Settings(home=tmp_path)
        with closing(connect(settings.database_path)) as db:
            migrate(db)
            store = TaskStore(db)
            engine = TruncatedEngine()
            supervisor = offline(settings, store, engine)
            try:
                tid = await start(supervisor, store)
                assert len(engine.prompts) == 2
                assert engine.prompts[1][0].text == CONTINUE_PROMPT
                assert [turn['stop_reason'] for turn in store.turns(tid)] == [reason, reason]
            finally:
                await supervisor.shutdown()
    asyncio.run(check())


@pytest.mark.parametrize('scenario', ['exit_resume', 'exit_recreate'])
def test_agent_exit_recovers_with_resume_or_recap(tmp_path, monkeypatch, scenario):
    upstream = tmp_path / 'repo'
    upstream.mkdir()
    subprocess.run(['git', 'init', '-q', str(upstream)], check=True)
    subprocess.run(['git', '-C', str(upstream), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.com',
                    'commit', '--allow-empty', '-qm', 'Initial'], check=True)
    script = Path(__file__).with_name('fake_acp_agent.py')
    monkeypatch.setitem(PROFILES, 'codex', replace(PROFILES['codex'],
        command=(sys.executable, str(script)), environment={'FAKE_ACP_SCENARIO': scenario}))

    async def check():
        settings = Settings(home=tmp_path / 'local')
        with closing(connect(settings.database_path)) as db:
            migrate(db)
            store = TaskStore(db)
            supervisor = TaskSupervisor(settings, store, issue_loader=issue_loader,
                sandbox_factories={'host': lambda: HostSandbox(settings, clone_source=str(upstream))})
            try:
                tid = await start(supervisor, store)
                previous_session = store.get(tid)['session_id']
                supervisor.send(tid, 'Previous conversation marker', 'previous')
                await until(lambda: store.get(tid)['agent_state'] == 'idle')
                crashed = supervisor.send(tid, 'Crash now', 'crash')
                await until(lambda: any(e.type == 'system.recovered' and e.payload['reason'] == 'agent_exit'
                                        for e in supervisor.events.history(tid)))
                assert next(t for t in store.turns(tid) if t['id'] == crashed['id'])['status'] == 'interrupted'
                assert (store.get(tid)['session_id'] != previous_session) == (scenario == 'exit_recreate')
                assert bool(store.get(tid)['recap_pending']) == (scenario == 'exit_recreate')
                continued = supervisor.continue_task(tid)
                await until(lambda: store.get(tid)['agent_state'] == 'idle')
                history = supervisor.events.history(tid)
                assert any(e.type == 'system.session_recreated' for e in history) == (scenario == 'exit_recreate')
                messages = [e.payload['text'] for e in history if e.type == 'message.agent' and e.turn_id == continued['id']]
                assert any(CONTINUE_PROMPT in message for message in messages)
                assert not store.get(tid)['recap_pending']
                if scenario == 'exit_recreate':
                    assert any(all(text in message for text in (
                        'You are a contribution agent', 'Previous conversation marker', 'Current edits', 'preserved.txt'))
                        for message in messages)
            finally:
                await supervisor.shutdown()
    asyncio.run(check())


def test_service_startup_restores_interrupted_sandbox_and_expires_card(tmp_path):
    class RestartedSandbox(FakeSandbox):
        kind = 'sbx'

        async def restore(self, task):
            restored.append(task.session_id)
            await super().restore(task)

    restored = []

    async def check():
        settings = Settings(home=tmp_path)
        with closing(connect(settings.database_path)) as db:
            migrate(db)
            store = TaskStore(db)
            tid = store.create(REF, 'codex', 'sbx')['id']
            store.ready(tid, {'title': 'Issue'}, 'main', 'base', 'work')
            original = store.claim(tid)
            store.session(tid, 'saved-session', {})
            engine = FakeEngine()
            supervisor = TaskSupervisor(settings, store, sandbox_factories={'sbx': RestartedSandbox},
                                        engine_factory=lambda *_: engine)
            supervisor.events.publish(tid, original['id'], EngineEvent('permission.requested',
                {'request_id': 'expired', 'options': [{'optionId': 'allow'}]}))
            try:
                supervisor.start()
                await until(lambda: any(event.type == 'system.recovered' for event in supervisor.events.history(tid)))
                assert restored == ['saved-session']
                assert engine.session_id == 'saved-session'
                assert store.turns(tid)[0]['status'] == 'interrupted'
                assert engine.prompts == []
                assert supervisor.events.pending(tid) == {}
                assert any(event.type == 'system.request_expired' and event.payload['request_id'] == 'expired'
                           for event in supervisor.events.history(tid))
                with pytest.raises(TaskError, match='expired'):
                    await supervisor.respond(tid, 'expired', RequestResponse(option_id='allow'))
                supervisor.continue_task(tid)
                await until(lambda: store.get(tid)['agent_state'] == 'idle')
                assert engine.prompts[0][0].text == CONTINUE_PROMPT
            finally:
                await supervisor.shutdown()
    asyncio.run(check())


def test_supervisor_limits_concurrent_running_turns(tmp_path):
    async def check():
        settings = Settings(home=tmp_path)
        with closing(connect(settings.database_path)) as db:
            migrate(db)
            store = TaskStore(db)
            engines = []

            def factory(*_):
                engine = FakeEngine(hold_turn=True)
                engines.append(engine)
                return engine

            supervisor = TaskSupervisor(settings, store, issue_loader=issue_loader,
                sandbox_factories={'host': FakeSandbox}, engine_factory=factory)
            try:
                ids = [supervisor.create(REF, isolation='host', trust_confirmed=True)['id'] for _ in range(3)]
                await until(lambda: [store.get(tid)['agent_state'] for tid in ids] == ['running', 'running', 'queued'])
                await until(lambda: len(engines) == 2 and all(e.running for e in engines))
                assert supervisor.interrupt(ids[0])
                await until(lambda: store.get(ids[2])['agent_state'] == 'running')
                assert store.get(ids[0])['agent_state'] == 'idle'
            finally:
                await supervisor.shutdown()
    asyncio.run(check())
