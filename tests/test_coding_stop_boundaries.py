"""6/8 boundaries, with a single evidence-qualified decision and fixed quotas."""
import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from app.agent_v2.coding_context import observe
from app.agent_v2.journal import AgentJournal
from app.agent_v2.loop import schedule
from app.workbench import Workbench
from tests.test_native_read_progress import setup, run_round


def snapshot(values):
    db, settings, task, sid = values[:4]
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        cp = j.checkpoint()
        return {"state": cp.state, "usage": cp.usage, "remaining": j.remaining(), "coding": observe(j)[0],
            "blocker": next((e.payload["failure"]["safe_reason_code"] for e in reversed(j.events())
                if isinstance(e.payload.get("failure"), dict)), None)}


def export(name, report):
    import os
    if folder := os.environ.get("CONTRIBOS_CODING_BOUNDARY_EVIDENCE"):
        from app.provenance import content_hash
        from app.security import ensure_no_sensitive_data
        value = {"classification": "deterministic_substitute_not_real_model_success", "case": name,
            "paid_model_calls": 0, **report}
        ensure_no_sensitive_data(value, context="stop boundary evidence")
        value["report_hash"] = content_hash(value)
        Path(folder).mkdir(parents=True, exist_ok=True)
        (Path(folder) / (name + ".json")).write_text(json.dumps(value, indent=2) + "\n")


def test_eight_attempted_identical_read_rounds_stop_at_existing_four_round_guard(tmp_path):
    values = setup(tmp_path, *[{} for _ in range(8)])
    model, tool, runner = values[4:7]
    for _ in range(4):
        run_round(model, tool)
    for _ in range(4):
        assert asyncio.run(model.run_once()) is None
    result = snapshot(values)
    assert result["state"] == "blocked" and result["usage"]["model_calls"] == 4
    assert result["blocker"] == "agent_no_progress" and len(runner.prompts) == 4
    export("identical_reads", result)


@pytest.mark.parametrize("critical_last", [False, True])
def test_eighth_new_page_requires_related_evidence_for_one_final_decision(tmp_path, critical_last):
    values = setup(tmp_path, *[{"offset": 128 * i, "max_bytes": 128} for i in range(8)])
    model, tool, runner, gateway = values[4:]
    gateway.text = "x" * 896 + "y" * 230
    if critical_last:
        # A valid Python file, with the only implementation anchor in page 8.
        # Reads target a genuinely approved write path, not the generic calc.py.
        gateway.text = "#" + "x" * 894 + "\n" + "def preserved_api():\n    return False\n" + "# padding\n" * 30
        db, settings, task, sid = values[:4]
        with db.session() as session:
            path = AgentJournal(Workbench(session, settings.artifact_root), task, sid).spec().write_paths[0]
        for value in runner.responses:
            call = value["choices"][0]["message"]["tool_calls"][0]
            call["function"]["arguments"] = json.dumps({**json.loads(call["function"]["arguments"]), "path": path})
    for _ in range(8):
        run_round(model, tool)
    before = snapshot(values)
    assert before["coding"]["available_source_bytes"] == 1024
    assert before["coding"]["delivered_source_bytes"] == 896
    assert runner.prompts[6]["implementation_progress"]["coding"]["correction_issued"]
    if critical_last:
        assert all("return False" not in json.dumps(p) for p in runner.prompts)
        with db.session() as session:
            j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
            last = j.wb.read(next(e.payload["artifact_id"] for e in reversed(j.events()) if e.kind == "agent_v2_tool_result"))
            assert "return False" in last["data"]["text"]
            from app.agent_v2.contracts import ToolCall
            from app.agent_v2.tools import RestrictedToolRegistry
            candidate = ToolCall(call_id="planned-ninth-patch", name="apply_patch", arguments={"path": path,
                "expected_hash": last["data"]["content_hash"], "old_text": "return False", "new_text": "return True"})
            RestrictedToolRegistry().validate(candidate, j.spec())
            assert candidate.arguments["expected_hash"] == hashlib.sha256(gateway.text.encode()).hexdigest()
            assert gateway.text.count(candidate.arguments["old_text"]) == 1
            from tests.test_agent_native_tools import response
            # This gateway only supplies reads. The actual ninth OCI Patch and
            # independent Review are exercised by the separate OCI journey.
            runner.responses.append(response("read_file", {"path": path, "offset": 1024, "max_bytes": 128}))
        run_round(model, tool)
        assert "return False" in json.dumps(runner.prompts[-1]["source_working_set"])
        before = snapshot(values)
    assert asyncio.run(model.run_once()).state == "succeeded"  # Platform stop, no model invocation.
    after = snapshot(values)
    assert after["state"] == "blocked" and after["blocker"] == "agent_coding_stalled"
    assert after["usage"] == before["usage"] and len(runner.prompts) == (9 if critical_last else 8)
    assert after["remaining"]["model_calls"] == (15 if critical_last else 16)
    export("critical_eighth_page" if critical_last else "continuous_new_pages", {"before_stop": before, "after_stop": after})


@pytest.mark.parametrize("responded", [False, True])
def test_unknown_model_dispatch_after_worker_loss_remains_blocked_and_charged(tmp_path, responded):
    from app.jobs import JobService
    values = setup(tmp_path, {})
    db, settings, task, sid, model, tool, runner, _ = values
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        jobs = JobService(session)
        job = jobs.lease_next(worker_id="lost-model", kinds=("agent_v2_model",))
        jobs.start(job.id, worker_id="lost-model")
        from app.agent_v2.loop import prompt_for
        prompt = prompt_for(j)
        j.append("model_started", {"prompt_artifact": j.wb.artifact(prompt)}, key="lost-native-start", job_id=job.id)
        cp = j.checkpoint()
        usage = j.charge(cp, {"model_calls": 1, "input_tokens": 1000, "output_tokens": 4096})
        j.transition(cp, state="model", usage=usage, key="lost-native-charge", job_id=job.id)
        if responded:
            from app.agent_v2.model_receipts import response_evidence
            from app.provenance import content_hash
            from tests.test_agent_native_tools import response
            binding = content_hash(j.spec().binding.model_dump())
            raw = response_evidence(response("list_paths", {}), request_id=job.id, binding_hash=binding)
            j.append("model_response", {"artifact_id": j.wb.artifact(raw), "prompt_artifact": content_hash(prompt),
                "binding_hash": binding}, key="lost-native-reply", job_id=job.id)
        jobs.fail(job.id, worker_id="lost-model", error_code="worker_lost", error_message="lost", commit=False)
        session.commit()
    # A fresh Journal is the same recovery path used by a restarted worker.
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        assert schedule(j) is None
        assert j.checkpoint().state == "blocked" and j.checkpoint().usage == usage
        session.commit()
    assert asyncio.run(model.run_once()) is None and asyncio.run(tool.run_once()) is None
    assert runner.prompts == []
    export("unknown_dispatch_with_response" if responded else "unknown_dispatch", snapshot(values))
