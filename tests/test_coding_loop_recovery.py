"""Offline recovery regressions. Deterministic responses do not prove autonomy."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent_v2.coding_context import observe, POLICY
from app.agent_v2.journal import AgentJournal
from app.agent_v2.loop import prompt_for
from app.agent_v2.native_tools import native_input_bound, native_model_transport, project_native_prompt
from app.provenance import content_hash
from app.workbench import Workbench
from tests.test_agent_native_tools import response
from tests.test_native_read_progress import setup, run_round

SAVED = Path("docs/agent-harness-reset-v1/read-batch-scheduler-v1/plan15-execution")


def observed(db, settings, task, sid):
    with db.session() as session:
        return observe(AgentJournal(Workbench(session, settings.artifact_root), task, sid))[0]


def test_new_source_receipts_cannot_reset_unpatched_round_limit(tmp_path):
    values = setup(tmp_path, *[{"offset": i * 128, "max_bytes": 128} for i in range(8)])
    db, settings, task, sid, model, tool, runner, gateway = values
    for _ in range(8):
        run_round(model, tool)
    assert runner.prompts[6]["implementation_progress"]["coding"]["correction_issued"]
    assert observed(db, settings, task, sid)["stop_required"]
    before = len(runner.prompts)
    assert asyncio.run(model.run_once()).state == "succeeded"
    assert len(runner.prompts) == before == POLICY["stop_after_unpatched_rounds"]
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        assert j.checkpoint().state == "blocked"
        assert j.checkpoint().usage["model_calls"] == 8
        assert j.remaining()["model_calls"] == 16
        assert j.remaining()["protocol_repairs"] == 2
        assert j.events()[-1].payload["progress"]["blocker"] == "agent_coding_stalled"
    assert asyncio.run(model.run_once()) is None


def test_available_source_is_not_delivered_until_a_response_confirms_the_prompt(tmp_path):
    db, settings, task, sid, model, tool, runner, gateway = setup(tmp_path, {}, {"offset": 4096})
    run_round(model, tool)
    first = observed(db, settings, task, sid)
    assert first["available_source_bytes"] == 4096 and first["delivered_source_bytes"] == 0
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        projection = prompt_for(j)
        assert projection["source_working_set"][0]["text"] == "a" * 4096
        assert native_input_bound(projection) <= 32000
    # Merely building a preview must not claim model delivery.
    assert observed(db, settings, task, sid) == first
    assert asyncio.run(model.run_once()).state == "succeeded"
    assert observed(db, settings, task, sid)["delivered_source_bytes"] == 4096
    # Recomputing through a fresh Journal yields identical counters/ranges.
    assert observed(db, settings, task, sid) == observed(db, settings, task, sid)


def test_duplicate_source_and_plan_never_create_coding_progress(tmp_path):
    db, settings, task, sid, model, tool, runner, gateway = setup(tmp_path, {}, {})
    run_round(model, tool)
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        aid = prompt_for(j)["task_context_artifact"]
    runner.responses.extend([response("read_artifact", {"artifact_id": aid})] * 2)
    for _ in range(3):
        run_round(model, tool)
    coding = observed(db, settings, task, sid)
    assert coding["available_source_bytes"] == 4096
    assert coding["delivered_source_bytes"] == 4096
    assert coding["overlapping_source_reads"] == 1
    assert coding["repeated_task_artifact_reads"] == 1
    assert coding["rounds_without_applied_patch"] == 4
    assert coding["rounds_without_new_delivered_source"] == 2


def archived_journal(round_number):
    """Read all saved bytes by content hash; never execute archived helpers."""
    from hashlib import sha256
    index = json.loads((SAVED / "evidence-index.json").read_text())
    paths = {r["id"]: SAVED / r["file"] for r in index["exported_artifacts"]}
    def read_bytes(aid):
        raw = paths[aid].read_bytes()
        assert sha256(raw).hexdigest() == aid
        return raw
    def read(aid):
        return json.loads(read_bytes(aid))
    starts = [e for e in index["events"] if e["kind"] == "agent_v2_model_started"]
    start = starts[round_number - 1]
    events = [SimpleNamespace(**{**e, "actor_id": "orchestrator"}) for e in index["events"]
        if e["sequence"] < start["sequence"]]
    prompt = read(start["payload"]["prompt_artifact"])
    j = SimpleNamespace(events=lambda: events, checkpoint=lambda: SimpleNamespace(source_hash=prompt["source_hash"]),
        spec=lambda: SimpleNamespace(write_paths=prompt["write_paths"]),
        wb=SimpleNamespace(read=read, artifacts=SimpleNamespace(read_bytes=read_bytes)))
    return j, prompt, read


@pytest.mark.parametrize("round_number", [7, 13])
def test_saved_plan15_source_is_delivered_instead_of_repeating_full_plan(round_number):
    journal, prompt, read = archived_journal(round_number)
    coding, pages = observe(journal)
    prompt["implementation_progress"]["coding"] = coding
    prompt["feedback"] = [{**read(row["receipt_artifact"]), "receipt_artifact": row["receipt_artifact"]}
        for row in prompt["feedback"]]
    transport = SimpleNamespace(model_transport=native_model_transport())
    projected = project_native_prompt(prompt, transport,
        history_artifact=prompt.get("history_evidence", {}).get("artifact_id"),
        task_input=read(prompt["task_context_artifact"]), source_pages=pages)
    assert native_input_bound(projected) <= 32000
    # Four useful excerpts, at least 3x the old uniform 128-byte/8-call projection.
    assert sum(len(p["text"].encode()) for p in projected["source_working_set"]) >= 3072
    assert projected["approved_task_input"]["brief"]["acceptance_criteria"] == read(prompt["task_context_artifact"])["approved_plan"]["acceptance_criteria"]
    for page in projected["source_working_set"]:
        original = read(page["receipt_artifact"])["data"]
        local = page["offset"] - original["offset"]
        assert page["text"].encode() == original["text"].encode()[local:local + len(page["text"].encode())]
    assert [r["receipt_artifact"] for r in projected["feedback"]] == [r["receipt_artifact"] for r in prompt["feedback"]]


def test_saved_plan15_historical_guard_diagnosis_does_not_resume_it():
    journal, _, _ = archived_journal(15)
    coding, _ = observe(journal)
    assert coding["model_rounds"] == coding["rounds_without_applied_patch"] == 14
    assert coding["stop_required"] and coding["first_patch_round"] is None


def test_saved_truncated_usage_is_retained_without_retry_or_tool_execution(tmp_path):
    from tests.test_native_protocol_repairs import fixture
    journal, _, read = archived_journal(15)
    index = json.loads((SAVED / "evidence-index.json").read_text())
    event = [e for e in index["events"] if e["kind"] == "agent_v2_model_response"][-1]
    saved = read(event["payload"]["artifact_id"])
    choice = saved["choices"][0]
    wire = {"choices": [{"finish_reason": choice["finish_reason"], "message": {
        "role": choice["role"], "content": None, "tool_calls": choice["tool_calls"]}}], "usage": saved["usage"]}
    db, settings, task, sid, model, tool, runner, gateway = fixture(tmp_path, wire)
    assert asyncio.run(model.run_once()).state == "succeeded"
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        started = next(e for e in j.events() if e.kind == "agent_v2_model_started")
        rejected = next(e for e in j.events() if e.kind == "agent_v2_model_rejected")
        captured = next(e for e in j.events() if e.kind == "agent_v2_model_response")
        assert j.wb.read(captured.payload["artifact_id"])["usage"] == saved["usage"]
        assert j.checkpoint().state == "blocked"
        assert rejected.payload["failure"]["safe_reason_code"] == "deepseek_completion_truncated"
        assert not rejected.payload["failure"]["retryable"]
        assert j.checkpoint().usage["input_tokens"] == started.payload["input_bound"]
        assert j.checkpoint().usage["output_tokens"] == 4096
        assert j.remaining()["provider_retries"] == 2
        assert observe(j)[0]["model_rounds"] == 1
    assert len(runner.prompts) == 1 and gateway.calls == []
    assert asyncio.run(model.run_once()) is None and asyncio.run(tool.run_once()) is None
