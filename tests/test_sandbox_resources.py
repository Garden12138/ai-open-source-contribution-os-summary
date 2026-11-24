import asyncio
from contextlib import closing

import pytest

from contribos.config import Settings
from contribos.db import connect, migrate
from contribos.sandbox.base import SandboxError
from contribos.sandbox.resources import writable_bytes
from contribos.tasks.store import TaskError, TaskStore
from contribos.tasks.supervisor import TaskSupervisor
from test_sandbox import FakeSbx, sbx_task
from test_task_supervisor import GatedEngine, REF, issue_loader, until


@pytest.mark.parametrize('at_clone', [True, False])
def test_resource_pause_while_preparing_can_rebuild_the_same_task(tmp_path, at_clone):
    class CloneSandbox(FakeSbx):
        async def _call(self, *args, **kwargs):
            if at_clone and 'clone' in args and not getattr(self, 'held_clone', False):
                self.held_clone = True
                self.writable_layer = 2 * 1024**2
                await until(lambda: self.disk_error)
                return 1, b'', b''
            return await super()._call(*args, **kwargs)

    async def check():
        settings = Settings(home=tmp_path)
        with closing(connect(settings.database_path)) as db:
            migrate(db)
            store = TaskStore(db)
            store.set_setting('resource_limits', {'disk_mib': 1})
            vm = CloneSandbox(tmp_path)
            vm.disk_check_interval = 0.01
            if not at_clone:
                vm.volume_bytes = None
            runner = TaskSupervisor(settings, store, issue_loader=issue_loader,
                sandbox_factories={'sbx': lambda: vm}, engine_factory=lambda *_: GatedEngine())
            try:
                tid = runner.create(REF, isolation='sbx')['id']
                await runner.runtimes[tid].job
                row = store.get(tid)
                expected = 'sandbox_disk_limit' if at_clone else 'sandbox_disk_unavailable'
                assert (row['status'], row['agent_state'], row['last_error_code']) == ('setup_failed', 'error', expected)
                assert row['sandbox_ref'] and not store.turns(tid)
                assert vm.exists and vm.state == 'stopped'
                vm.writable_layer = vm.volume_bytes = 0
                assert runner.continue_task(tid) is None
                await runner.runtimes[tid].job
                assert store.get(tid)['status'] == 'ready'
                assert len(store.turns(tid)) == 1 and store.turns(tid)[0]['status'] == 'completed'
                assert sum(call[0] == 'create' for call in vm.received) == 2
                assert sum(call[0] == 'rm' for call in vm.received) == 1
            finally:
                await runner.shutdown()
    asyncio.run(check())


def test_idle_sandbox_stops_on_schedule_and_resumes_with_a_fresh_disk_check(tmp_path):
    async def check():
        settings = Settings(home=tmp_path)
        with closing(connect(settings.database_path)) as db:
            migrate(db)
            store = TaskStore(db)
            vm = FakeSbx(tmp_path)
            runner = TaskSupervisor(settings, store, issue_loader=issue_loader,
                sandbox_factories={'sbx': lambda: vm}, engine_factory=lambda *_: GatedEngine())
            try:
                tid = runner.create(REF, isolation='sbx')['id']
                runtime = runner.runtimes[tid]
                await runtime.job
                now = runtime.last_activity
                runner._check_idle(now + 1799)
                assert runtime.engine is not None
                runner._check_idle(now + 1800)
                await runtime.job
                assert runtime.engine is None and not runtime.sandbox_suspended
                runner._check_idle(now + 7200)
                await runtime.job
                assert vm.state == 'stopped' and runtime.sandbox_suspended
                assert vm.exists
                vm.received.clear()
                runner.continue_task(tid)
                await runtime.job
                assert ('df', '--verbose', '--json') in vm.received
                assert not runtime.sandbox_suspended and store.get(tid)['agent_state'] == 'idle'
            finally:
                await runner.shutdown()
    asyncio.run(check())


def test_sbx_df_accounts_for_the_task_layer_and_dedicated_volume():
    container = {'name': 'ctask-one', 'size': 100, 'local_volumes': 1}
    volume = {'name': 'ctask-one-docker', 'size': 50}
    report = {'containers': [container, {'name': 'ctask-two', 'size': 5000, 'local_volumes': 0}],
              'volumes': [volume, {'name': 'ctask-two-docker', 'size': 800}]}
    assert writable_bytes(report, 'ctask-one') == 150
    for invalid in ({'size': None}, {'size': -1}, {'local_volumes': 2}, {'name': 'missing'}):
        with pytest.raises(SandboxError, match='sandbox_disk_unavailable'):
            writable_bytes({**report, 'containers': [{**container, **invalid}]}, 'ctask-one')


def test_stop_after_restart_leaves_a_workspace_owned_by_a_cli_session_running(tmp_path):
    async def check():
        owner, recovered = FakeSbx(tmp_path), FakeSbx(tmp_path)
        task = sbx_task()
        await owner.prepare(task)
        try:
            with pytest.raises(SandboxError, match='task_already_open'):
                await recovered.stop_saved(task)
            assert owner.state == 'running' and owner.exists
            assert not recovered.received
        finally:
            await owner.destroy()
    asyncio.run(check())


@pytest.mark.parametrize('usage,error', [(1024**2 + 1, 'sandbox_disk_limit'), (None, 'sandbox_disk_unavailable')])
def test_sandbox_resource_pause_stops_vm_and_holds_queue_across_restart(tmp_path, usage, error):
    async def run():
        settings = Settings(home=tmp_path)
        with closing(connect(settings.database_path)) as db:
            migrate(db)
            store = TaskStore(db)
            store.set_setting('resource_limits', {'cpus': 3, 'memory_mib': 2048, 'disk_mib': 1})
            vm = FakeSbx(tmp_path)
            vm.disk_check_interval = 0.02
            def supervisor(engine):
                return TaskSupervisor(settings, store, issue_loader=issue_loader,
                    sandbox_factories={'sbx': lambda: vm}, engine_factory=lambda *_: engine)
            engine = GatedEngine()
            runner = supervisor(engine)
            try:
                tid = runner.create(REF, isolation='sbx')['id']
                await until(lambda: store.get(tid)['status'] == 'ready' and store.get(tid)['agent_state'] == 'idle')
                create = next(cmd for cmd in vm.received if cmd[0] == 'create')
                assert create[create.index('--cpus') + 1] == '3'
                assert create[create.index('--memory') + 1] == '2048m'
                runner.send(tid, 'hold-test')
                runner.send(tid, 'queued before limit')
                await until(lambda: 'hold-test' in engine.calls)
                vm.volume_bytes = usage
                await until(lambda: store.get(tid)['last_error_code'] == error and vm.state == 'stopped')
                await until(lambda: runner.runtimes[tid].job.done())
                assert store.get(tid)['agent_state'] == 'error'
                assert [r['status'] for r in store.turns(tid)][-2:] == ['interrupted', 'queued']
                assert 'queued before limit' not in engine.calls
                assert db.execute("SELECT count(*) FROM task_events WHERE task_id=? AND type='system.runtime_failed'",
                                  (tid,)).fetchone()[0] == 1
                with pytest.raises(TaskError) as rejected:
                    runner.background(tid, lambda: asyncio.sleep(0))
                assert rejected.value.code == error
            finally:
                await runner.shutdown()

            recovered = FakeSbx(tmp_path)
            recovered.exists = True
            recovered.state = 'running'
            recovered.scoped_rules = vm.scoped_rules
            recovered.volume_bytes = usage
            vm = recovered
            engine = GatedEngine()
            runner = supervisor(engine)
            try:
                runner.start()
                await runner.runtimes[tid].job
                assert recovered.state == 'stopped' and recovered.exists
                assert not engine.calls and not any(cmd[0] == 'exec' for cmd in recovered.received)
                runner.continue_task(tid)
                await until(lambda: runner.runtimes[tid].job.done())
                assert not engine.calls and vm.state == 'stopped'
                assert store.get(tid)['last_error_code'] == error
                await until(lambda: vm._monitor.done())
                vm.volume_bytes = 1024**2
                runner.continue_task(tid)
                await until(lambda: store.get(tid)['agent_state'] == 'idle')
                assert 'queued before limit' in engine.calls
                runner.delete(tid)
                await until(lambda: tid not in runner.runtimes)
                assert not vm.exists and vm.lock is None and vm._monitor.done()
            finally:
                await runner.shutdown()
    asyncio.run(run())
