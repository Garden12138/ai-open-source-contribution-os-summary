"""A stop must cover any successor created by an in-flight coordinator tick."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from app.contribution_workflow import _advance, stop_workflow
from app.workbench import Workbench
from tests.test_workbench import automatic_fixture


def test_stop_and_preparation_successor_are_serialized(tmp_path, monkeypatch):
    from app.sandbox_worker import workspace_jobs
    db, settings, task, workers, _, _ = automatic_fixture(tmp_path)
    assert asyncio.run(workers[0].run_once()).state == 'succeeded'
    reached, release, stop_started = threading.Event(), threading.Event(), threading.Event()
    original = workspace_jobs.preparation_receipt

    def delayed_receipt(wb, attempt_id):
        if threading.current_thread().name.startswith('coordinator'):
            reached.set()
            assert release.wait(5), 'Test coordinator gate timed out'
        return original(wb, attempt_id)

    monkeypatch.setattr(workspace_jobs, 'preparation_receipt', delayed_receipt)

    def advance():
        with db.session() as session:
            _advance(Workbench(session, settings.artifact_root), task, settings)
            session.commit()

    def stop():
        stop_started.set()
        with db.session() as session:
            stop_workflow(Workbench(session, settings.artifact_root), task, key='concurrent-stop')

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='coordinator') as coordinator, ThreadPoolExecutor(max_workers=1) as stopper:
        advancing = coordinator.submit(advance)
        assert reached.wait(5)
        stopping = stopper.submit(stop)
        assert stop_started.wait(5)
        # Allow the competing stop to acquire its transaction if advancement has
        # not already serialized it. Release even if stop correctly waits.
        timer = threading.Timer(.2, release.set)
        timer.start()
        try:
            advancing.result(timeout=10)
            stopping.result(timeout=10)
        finally:
            release.set()
            timer.cancel()
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        stopped = wb.latest(task, 'stop_requested')
        successors = [e for e in wb.history(task) if e.kind == 'job_queued' and e.payload['kind'] == 'execution_prepare']
        assert all(e.sequence < stopped.sequence for e in successors)
        assert {e.job_id for e in successors} <= set(stopped.payload['job_ids'])
        assert all(j.state == 'cancelled' for j in wb.jobs(task) if j.kind == 'execution_prepare')
