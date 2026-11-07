"""Real credential-free OCI ninth Patch/Verify, scripted independent Review."""
import asyncio
import hashlib
import json
import os

import pytest

from app.agent_v2.coding_context import observe, handoff_used
from app.agent_v2.integration import advance_coding
from app.agent_v2.journal import AgentJournal
from app.agent_v2.native_tools import FINISH
from app.contribution_workflow import advance_workflows
from app.model_settings import task_identity
from app.workbench import Workbench
from tests.test_agent_native_tools import response
from tests.test_coding_stop_boundaries import snapshot, export
from tests.test_coding_stop_boundaries_oci import real_session
from tests.test_harness_progress import progress
from tests.test_native_read_progress import run_round

pytestmark = pytest.mark.skipif(os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1",
    reason="Requires fixed local OCI image; no paid model")


@pytest.mark.parametrize("rejected", [False, True])
def test_ninth_oci_patch_must_change_source_then_verify_and_independent_review(tmp_path, monkeypatch, rejected):
    workers = []
    values, source = real_session(tmp_path, monkeypatch, capture_workers=workers)
    db, settings, task, sid, model, tool, runner, _ = values
    assert asyncio.run(workers[1].run_once()).state == "succeeded"
    path = "app/plans.py"
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        attempt = j.spec().binding.attempt_id
        workers[3].identity = task_identity(session, settings, task, "review")
    runner.responses.extend(response("read_file", {"path": path, "offset": 128 * i, "max_bytes": 128}) for i in range(8))
    for _ in range(8): run_round(model, tool)
    assert snapshot(values)["coding"]["handoff_candidate"]
    runner.responses.append(response("apply_patch", {"path": path,
        "expected_hash": "0" * 64 if rejected else hashlib.sha256(source.encode()).hexdigest(),
        "old_text": "return True", "new_text": "return True  # offline ninth patch"}))
    run_round(model, tool)
    assert runner.prompts[-1]["coding_handoff"]["inline_ranges"] == [[896, 1024]]
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        assert handoff_used(j)
        receipt = j.wb.read(next(e.payload["artifact_id"] for e in reversed(j.events()) if e.kind == "agent_v2_tool_result"))
        assert receipt["data"]["execution_backend"] == "docker"
        assert (receipt["source_hash"] != receipt["output_source_hash"]) == (not rejected)
    if rejected:
        assert receipt["status"] == "rejected"
        before = snapshot(values)
        assert asyncio.run(model.run_once()).state == "succeeded"
        after = snapshot(values)
        assert after["usage"] == before["usage"] and len(runner.prompts) == 9
        assert after["state"] == "blocked" and after["coding"]["first_patch_round"] is None
        export("v11_oci_rejected_ninth_patch", {"actual_OCI": True, "after_stop": after})
        return
    assert receipt["status"] == "succeeded"
    assert snapshot(values)["coding"]["first_patch_round"] == 9
    runner.responses.extend([response("run_approved_checks", {}), response(FINISH, {"reply": "independent review requested"})])
    run_round(model, tool)
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        check = j.wb.read(next(e.payload["artifact_id"] for e in reversed(j.events()) if e.kind == "agent_v2_tool_result"))
        assert check["data"]["outcome"] == "passed" and check["output_source_hash"] == j.checkpoint().source_hash
    assert asyncio.run(model.run_once()).state == "succeeded"
    with db.session() as session: advance_coding(Workbench(session, settings.artifact_root), attempt, settings)
    for _ in range(2): assert asyncio.run(workers[0].run_once()).state == "succeeded"
    advance_workflows(db, settings)
    workers[3].runner.contents.append(json.dumps({"verdict": "pass", "reason_code": "evidence_checked", "findings": [
        {"severity": "info", "location": path, "evidence": "Frozen diff and latest source Verify checked",
            "recommendation": "Human acceptance", "verdict": "pass"}]}))
    reviewed = asyncio.run(workers[3].run_once())
    assert reviewed.state == "succeeded", reviewed.error_message
    envelope = json.loads(workers[3].runner.invocations[-1].prompt.split("=", 1)[1])
    assert envelope["unified_diff"] and envelope["normalized_test_results"]
    advance_workflows(db, settings)
    assert progress(db, settings, task, sid)[0]["phase"] == "ready_for_human"
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        assert observe(j)[0]["first_patch_round"] == 9 and handoff_used(j)
        assert not j.wb.latest(task, "execution_accepted") and not j.wb.latest(task, "publication_confirmed")
    export("v11_oci_ninth_patch_verify_independent_review", {"actual_OCI": True,
        "independent_review": "deterministic_substitute_passed", "human_acceptance": "pending", "result": snapshot(values)})
