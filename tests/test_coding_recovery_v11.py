"""6+8+1 admission and recovery with deterministic model decisions, no provider."""
import asyncio
import json

import pytest

from app.agent_v2.journal import AgentJournal
from app.agent_v2.loop import AgentModelWorker, prompt_for, schedule
from app.agent_v2.coding_context import observe, handoff_used
from app.workbench import Workbench
from tests.test_agent_native_tools import response
from tests.test_native_read_progress import setup, run_round
from tests.test_coding_stop_boundaries import snapshot, export


def investigation(tmp_path, *, last="new", related=True):
    values = setup(tmp_path, *[{"offset": 128 * i, "max_bytes": 128} for i in range(8)])
    db, settings, task, sid, model, tool, runner, gateway = values
    gateway.text = "#" + "x" * 894 + "\ndef preserved_api():\n    return False\n" + "# more evidence\n" * 100
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        path = j.spec().write_paths[0] if related else "unplanned/calc.py"
    for value in runner.responses:
        c = value["choices"][0]["message"]["tool_calls"][0]
        c["function"]["arguments"] = json.dumps({**json.loads(c["function"]["arguments"]), "path": path})
    if last == "repeat":
        runner.responses[-1] = response("read_file", {"path": path, "offset": 0, "max_bytes": 128})
    elif last == "index":
        for _ in range(7): run_round(model, tool)
        with db.session() as session:
            j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
            aid = next(e.payload["artifact_id"] for e in reversed(j.events()) if e.kind == "agent_v2_progress")
        runner.responses[-1] = response("read_artifact", {"artifact_id": aid})
    elif last == "hash":
        execute = gateway.execute
        async def hash_only(spec, call, args, **kw):
            value = await execute(spec, call, args, **kw)
            if len(gateway.calls) == 8:
                value["result"]["data"].pop("text")
            return value
        gateway.execute = hash_only
    for _ in range(1 if last == "index" else 8): run_round(model, tool)
    return values, path


def test_new_eighth_source_is_in_actual_ninth_prompt_and_cannot_extend_again(tmp_path):
    values, path = investigation(tmp_path)
    db, settings, task, sid, model, tool, runner, _ = values
    assert snapshot(values)["coding"]["handoff_candidate"]
    runner.responses.append(response("read_file", {"path": path, "offset": 1024, "max_bytes": 128}))
    # Fresh worker instance: admission uses committed evidence, not a RAM flag.
    restarted = AgentModelWorker(db, settings, model.adapter_factory, worker_id="ninth-after-restart")
    run_round(restarted, tool)
    assert "return False" in json.dumps(runner.prompts[-1]["source_working_set"])
    assert runner.prompts[-1]["coding_handoff"]["evidence"]["ranges"] == [[896, 1024]]
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        assert handoff_used(j)
        assert observe(j)[0]["delivered_source_bytes"] >= 1024
    before = snapshot(values)
    assert asyncio.run(restarted.run_once()).state == "succeeded"
    after = snapshot(values)
    assert after["state"] == "blocked" and after["usage"] == before["usage"]
    assert len(runner.prompts) == after["usage"]["model_calls"] == 9
    export("v11_ninth_read_no_extension", after)


@pytest.mark.parametrize("last,related", [("repeat", True), ("index", True), ("hash", True), ("new", False)])
def test_no_opportunity_from_repeat_index_hash_or_unrelated_source(tmp_path, last, related):
    values, _ = investigation(tmp_path, last=last, related=related)
    before = snapshot(values)
    assert before["coding"]["handoff_candidate"] is None
    assert asyncio.run(values[4].run_once()).state == "succeeded"
    after = snapshot(values)
    assert after["state"] == "blocked" and after["usage"] == before["usage"]
    assert len(values[6].prompts) == 8
    export("v11_ineligible_" + last + ("_unrelated" if not related else ""), after)


@pytest.mark.parametrize("fault", ["oversized", "missing_bytes", "forged_bytes", "missing_marker", "path", "next_offset", "source_page_artifact", "inline_ranges"])
def test_failed_exact_prompt_admission_never_invokes_or_charges_ninth(tmp_path, monkeypatch, fault):
    values, _ = investigation(tmp_path)
    before = snapshot(values)
    def invalid(journal):
        p = prompt_for(journal)
        if fault == "oversized": p["unshrinkable"] = "x" * 33000
        elif fault == "missing_bytes": p["source_working_set"] = []
        elif fault == "forged_bytes": p["source_working_set"][0]["text"] = "invented source" * 20
        elif fault == "missing_marker": p.pop("coding_handoff")
        elif fault == "inline_ranges": p["coding_handoff"]["inline_ranges"] = []
        else: p["source_working_set"][0][fault] = "forged provenance"
        return p
    monkeypatch.setattr("app.agent_v2.loop.prompt_for", invalid)
    assert asyncio.run(values[4].run_once()).state == "succeeded"
    after = snapshot(values)
    assert after["state"] == "blocked" and after["blocker"] == "agent_coding_handoff_unavailable"
    assert after["usage"] == before["usage"] and len(values[6].prompts) == 8


def test_unknown_ninth_result_consumes_opportunity_and_never_replays(tmp_path):
    values, _ = investigation(tmp_path)
    db, settings, task, sid, model, _, runner, _ = values
    runner.responses.append(RuntimeError("lost ninth response"))
    assert asyncio.run(model.run_once()).state == "failed"
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        assert handoff_used(j) and j.checkpoint().usage["model_calls"] == 9
        charged = dict(j.checkpoint().usage)
        assert schedule(j) is None
        assert j.checkpoint().state == "blocked" and j.checkpoint().usage == charged
        session.commit()
    assert asyncio.run(model.run_once()) is None and len(runner.prompts) == 9
    export("v11_unknown_ninth", snapshot(values))


def test_old_unseen_bytes_do_not_become_new_eighth_round_evidence(tmp_path, monkeypatch):
    def without_source(journal):
        p = prompt_for(journal)
        p["source_working_set"] = []
        for row in p.get("feedback", []): row.get("data", {}).pop("text", None)
        return p
    monkeypatch.setattr("app.agent_v2.loop.prompt_for", without_source)
    # Page 8 overlaps already observed page 1, despite a different request key
    # and despite no source having reached the substitute model at all.
    values = setup(tmp_path, *[{"offset": 128 * i, "max_bytes": 128} for i in range(7)], {"offset": 1, "max_bytes": 128})
    db, settings, task, sid, model, tool, runner, _ = values
    with db.session() as session:
        path = AgentJournal(Workbench(session, settings.artifact_root), task, sid).spec().write_paths[0]
    for value in runner.responses:
        call = value["choices"][0]["message"]["tool_calls"][0]
        call["function"]["arguments"] = json.dumps({**json.loads(call["function"]["arguments"]), "path": path})
    for _ in range(8): run_round(model, tool)
    state = snapshot(values)
    assert state["coding"]["delivered_source_bytes"] == 0
    assert state["coding"]["handoff_candidate"] is None
    assert asyncio.run(model.run_once()).state == "succeeded" and len(runner.prompts) == 8


def test_handoff_does_not_allocate_budget_and_consumption_spans_same_authorization(tmp_path, monkeypatch):
    values, path = investigation(tmp_path)
    original = AgentJournal.remaining
    def insufficient(j):
        return {**original(j), "input_tokens": 0}
    monkeypatch.setattr(AgentJournal, "remaining", insufficient)
    before = snapshot(values)["usage"]
    assert asyncio.run(values[4].run_once()).state == "succeeded"
    assert snapshot(values)["usage"] == before and len(values[6].prompts) == 8
    monkeypatch.undo()
    # A separate fixture actually dispatches the exception, then reconstructs
    # a child-session view. Task history, not session RAM, consumes it.
    second, path = investigation(tmp_path / "consumed")
    second[6].responses.append(response("read_file", {"path": path, "offset": 1024, "max_bytes": 128}))
    run_round(second[4], second[5])
    from types import SimpleNamespace
    with second[0].session() as session:
        j = AgentJournal(Workbench(session, second[1].artifact_root), second[2], second[3])
        child = SimpleNamespace(spec=j.spec, wb=j.wb, task_id=j.task_id, events=lambda: [])
        assert handoff_used(child)
