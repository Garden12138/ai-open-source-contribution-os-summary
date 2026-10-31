"""Frozen environment declarations precede model planning without extra authority."""
import asyncio
import hashlib
import json

import pytest

from app.archives import RepositoryArchiveStore
from app.planner import PlanningContextWorker, PlanningService, PlanningTurnWorker, frozen_inputs
from app.planning_budget import planning_usage
from tests.test_workbench import seeded


def entry(path, content):
    return {"path": path, "content": content, "prior_hash": hashlib.sha256(content.encode()).hexdigest(),
            "executable": False}


def node_context(wb, task, context, lock, *, captured=False, unavailable=False, pressure=False):
    entries = [entry("package.json", json.dumps({"name": "fixture", "private": True,
        "packageManager": "pnpm@9.15.4" if lock == "pnpm-lock.yaml" else "npm@10.8.2"}))]
    lock_entry = entry(lock, "x" * 100_000 if pressure else "lock declaration")
    if captured or unavailable:
        entries.append({**lock_entry, "content": None} if unavailable else lock_entry)
    if pressure:
        entries += [entry(f"speculative-{i}.ts", "x") for i in range(46)]
    evidence = {"inventory": sorted({e['path'] for e in entries} | {lock, "source.ts"}),
                "entries": entries, "truncated": False}
    return wb.append(task, "context_ready", {**context.payload, "artifact_id": wb.artifact(evidence)},
                     key="node-context")


@pytest.mark.parametrize("lock", ["pnpm-lock.yaml", "package-lock.json"])
def test_missing_root_declaration_is_captured_before_first_model_call(tmp_path, monkeypatch, lock):
    monkeypatch.setenv("UPEVE_ENABLED", "1")
    monkeypatch.setenv("UPEVE_TARGET_PLATFORM", "linux/arm64")
    db, settings, task, _, context = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        context = node_context(wb, task, context, lock)
        history = [(e.id, e.record_hash) for e in wb.history(task)]
        before = planning_usage(wb, task)
        job = wb.request(task, text="Prepare plan", parent_id=None, expected_hash=None,
                         key="root-declaration", runtime_version="agent-runtime-v2")
        assert job.kind == "planning_context" and job.payload["paths"] == [lock]
        assert job.payload["round"] == 0
        assert wb.request(task, text="Prepare plan", parent_id=None, expected_hash=None,
                          key="root-declaration", runtime_version="agent-runtime-v2").id == job.id
        assert planning_usage(wb, task) == before

    class Runtime:
        async def inspect(self, **kwargs):
            assert kwargs["paths"] == (lock,)
            return {"inventory": ["package.json", lock, "source.ts"],
                    "entries": [entry(lock, "lock declaration")], "truncated": False}

    monkeypatch.setattr(RepositoryArchiveStore, "lookup", lambda *a, **kw: object())
    completed = asyncio.run(PlanningContextWorker(db, settings, Runtime(), worker_id="declarations").run_once())
    assert completed.state == "succeeded", completed.error_message
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert [(e.id, e.record_hash) for e in wb.history(task)[:len(history)]] == history
        child = next(j for j in wb.jobs(task) if j.id == completed.result_data["next_job_id"])
        assert child.kind == "planning_turn" and child.payload["round"] == 0
        assert planning_usage(wb, task) == before
        assert wb.latest(task, "execution_authorized") is None and wb.latest_plan(task) is None
    db.close()


def test_exhausted_reads_report_exact_gap_without_claiming_repairs_exhausted(tmp_path, monkeypatch):
    from tests.test_planning_reads import read_fixture
    from tests.test_workbench import result, run_turn

    db, settings, task, content = read_fixture(tmp_path)
    with db.session() as session:
        PlanningService(session, settings.artifact_root).request(task, text="Prepare plan", parent_id=None,
            expected_hash=None, key="read-limit")
    monkeypatch.setattr("app.planner.MAX_READ_ROUNDS", 0)
    reply = json.loads(result(content))
    reply["plan"]["files_to_inspect"].append("safe.py")
    completed, _ = run_turn(db, settings, json.dumps(reply))
    assert completed.state == "succeeded", completed.error_message
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        last = wb.latest(task, "assistant_message").payload
        assert last["read_feedback"]["required_paths"] == ["safe.py"]
        assert last["read_feedback"]["read_round"] == last["read_feedback"]["maximum_read_rounds"] == 0
        assert "读取轮数上限" in last["reply"] and "修订上限" not in last["reply"]
        assert len(wb.jobs(task)) == 1 and wb.latest_plan(task) is None
        assert wb.latest(task, "execution_authorized") is None
    db.close()


@pytest.mark.parametrize("round_number", [0, 4])
def test_archive_continuation_prefetches_within_existing_round_cap(tmp_path, round_number):
    from tests.test_nvidia_workflows import ScriptedRunner, _identity

    db, settings, task, _, context = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        context = node_context(wb, task, context, "pnpm-lock.yaml")
        wb.enqueue(task, "planning_turn", {"state_hash": wb.require_planning(task),
            "context_id": context.id, "inputs": frozen_inputs(session, task), "text": "Prepare plan",
            "parent_id": None, "parent_hash": None, "round": round_number,
            "agent_runtime_version": "agent-runtime-v2", "environment_engine": {"version": "upeve-v1"}},
            key="archive-continuation")
    runner = ScriptedRunner(json.dumps({"questions": [{"id": "source", "prompt": "Missing source?", "options": []}]}))
    worker = PlanningTurnWorker(db, settings, runner, _identity(settings.implementation_model), worker_id="prefetch")
    completed = asyncio.run(worker.run_once())
    assert completed.state == "succeeded", completed.error_message
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        if round_number == 0:
            assert not runner.invocations and completed.result_data["environment_sources_requested"]
            child = next(j for j in wb.jobs(task) if j.kind == "planning_context")
            assert child.payload["paths"] == ["pnpm-lock.yaml"] and child.payload["round"] == 1
            assert planning_usage(wb, task)["calls"] == 0
        else:
            assert len(runner.invocations) == 1
            assert not any(j.kind == "planning_context" for j in wb.jobs(task))
            assert runner.invocations[0].output_schema["properties"]["read_paths"]["maxItems"] == 0
        assert wb.latest_plan(task) is None and wb.latest(task, "execution_authorized") is None
    db.close()


@pytest.mark.parametrize("v2,enabled", [(False, True), (True, False)])
def test_legacy_or_disabled_requests_keep_existing_route(tmp_path, monkeypatch, v2, enabled):
    monkeypatch.setenv("UPEVE_ENABLED", "1" if enabled else "0")
    monkeypatch.setenv("UPEVE_TARGET_PLATFORM", "linux/arm64")
    db, settings, task, _, context = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        node_context(wb, task, context, "pnpm-lock.yaml")
        job = wb.request(task, text="Prepare plan", parent_id=None, expected_hash=None, key="legacy",
                         **({"runtime_version": "agent-runtime-v2"} if v2 else {}))
        assert job.kind == "planning_turn"
    db.close()


def test_unavailable_declaration_is_never_repeated_as_a_read(tmp_path, monkeypatch):
    monkeypatch.setenv("UPEVE_ENABLED", "1")
    monkeypatch.setenv("UPEVE_TARGET_PLATFORM", "linux/arm64")
    db, settings, task, _, context = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        node_context(wb, task, context, "pnpm-lock.yaml", unavailable=True)
        job = wb.request(task, text="Prepare plan", parent_id=None, expected_hash=None, key="unavailable",
                         runtime_version="agent-runtime-v2")
        assert job.kind == "planning_turn"
        assert job.payload["read_feedback"]["unavailable_paths"] == ["pnpm-lock.yaml"]
        assert wb.latest_plan(task) is None
    db.close()


def test_later_source_capture_preserves_root_declarations_under_existing_pressure(tmp_path, monkeypatch):
    db, settings, task, _, context = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        context = node_context(wb, task, context, "pnpm-lock.yaml", captured=True, pressure=True)
        wb.enqueue(task, "planning_context", {"state_hash": wb.require_planning(task),
            "context_id": context.id, "inputs": frozen_inputs(session, task), "text": "Read source",
            "paths": ["source.ts"], "round": 1, "environment_engine": {"version": "upeve-v1"},
            "agent_runtime_version": "agent-runtime-v2",
            **{k: context.payload[k] for k in ("archive_hash", "base_sha", "branch", "image")}}, key="source")

    class Runtime:
        async def inspect(self, **kwargs):
            return {"inventory": ["package.json", "pnpm-lock.yaml", "source.ts",
                                   *[f"speculative-{i}.ts" for i in range(46)]],
                    "entries": [entry("source.ts", "source")], "truncated": False}

    monkeypatch.setattr(RepositoryArchiveStore, "lookup", lambda *a, **kw: object())
    completed = asyncio.run(PlanningContextWorker(db, settings, Runtime(), worker_id="source").run_once())
    assert completed.state == "succeeded", completed.error_message
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        current = wb.read(wb.latest(task, "context_ready").payload["artifact_id"])
        paths = {e["path"] for e in current["entries"]}
        assert {"package.json", "pnpm-lock.yaml", "source.ts"} <= paths
        assert len(paths) == 48
        assert wb.latest(task, "execution_authorized") is None
    db.close()
