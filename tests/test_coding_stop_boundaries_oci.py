"""Real offline OCI operations with synthetic authority and scripted decisions."""
import asyncio
from dataclasses import replace
import hashlib
import io
import os
import tarfile

import pytest

from app.agent_v2.journal import AgentJournal
from app.agent_v2.loop import AgentToolWorker
from app.agent_v2.native_tools import FINISH
from app.agent_v2.coding_context import observe
from app.plans import PlanCommand
from app.sandbox_worker.agent_tools import DockerAgentToolGateway
from app.workbench import Workbench
from tests.test_agent_native_tools import response
from tests.test_coding_stop_boundaries import snapshot, export
from tests.test_native_protocol_repairs import fixture
from tests.test_native_read_progress import run_round

pytestmark = pytest.mark.skipif(os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1",
    reason="Requires fixed local OCI image; never real model success")


def real_session(tmp_path, monkeypatch, *, capture_workers=None):
    import tests.test_workbench as workbench
    import tests.test_execution_pipeline as pipeline
    original_settings, original_plan = workbench.Settings, workbench._plan_content
    def settings(*args, **kwargs):
        return original_settings(*args, **{**kwargs, "workbench_runner_image": os.environ["CONTRIBOS_SANDBOX_RUNNER_IMAGE"]})
    def plan(*args, **kwargs):
        return replace(original_plan(*args, **kwargs), commands_to_run=(PlanCommand(command_id="unit",
            purpose="offline fixture contract", argv=("python3", "-m", "unittest", "discover", "-s", "tests")),))
    monkeypatch.setattr(workbench, "Settings", settings)
    monkeypatch.setattr(workbench, "_plan_content", plan)
    source = "def preserved_api():\n    return True\n" + "# offline source evidence\n" * 150
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for path, text in {"app/__init__.py": "", "app/plans.py": source,
            "app/planning.py": "# inspection fixture\n",
            "tests/test_planning.py": "import unittest\nfrom app.plans import preserved_api\nclass Contract(unittest.TestCase):\n    def test_contract(self): self.assertTrue(preserved_api())\n"}.items():
            member = tarfile.TarInfo("fixture-" + pipeline.BASE_SHA + "/" + path)
            member.size, member.mode = len(text.encode()), 0o644
            archive.addfile(member, io.BytesIO(text.encode()))
    monkeypatch.setattr(pipeline, "ARCHIVE_BYTES", stream.getvalue())
    values = list(fixture(tmp_path, capture_workers=capture_workers))
    db, config, task, sid = values[:4]
    environment = {k: os.environ[k] for k in ("HOME", "PATH", "TMPDIR", "DOCKER_CONTEXT", "DOCKER_HOST") if k in os.environ}
    gateway = DockerAgentToolGateway(artifact_root=config.artifact_root, docker_environment=environment,
        repository_for_attempt=lambda _: "fixture/fixture")
    values[5] = AgentToolWorker(db, config, gateway, worker_id="real-offline-boundary")
    values[7] = gateway
    return values, source


@pytest.mark.parametrize("rejected", [False, True])
def test_real_oci_sixth_patch_resets_guard_but_rejected_eighth_patch_does_not(tmp_path, monkeypatch, rejected):
    values, source = real_session(tmp_path, monkeypatch)
    db, config, task, sid, model, tool, runner, _ = values
    reads = 7 if rejected else 5
    runner.responses.extend(response("read_file", {"path": "app/plans.py", "offset": 128 * i, "max_bytes": 128}) for i in range(reads))
    runner.responses.append(response("apply_patch", {"path": "app/plans.py",
        "expected_hash": "0" * 64 if rejected else hashlib.sha256(source.encode()).hexdigest(),
        "old_text": "return True", "new_text": "return True  # exact offline edit"}))
    for _ in range(reads + 1):
        run_round(model, tool)
    result = snapshot(values)
    with db.session() as session:
        j = AgentJournal(Workbench(session, config.artifact_root), task, sid)
        latest = j.wb.read(next(e.payload["artifact_id"] for e in reversed(j.events()) if e.kind == "agent_v2_tool_result"))
        assert latest["data"].get("execution_backend") == "docker"
        if rejected:
            assert latest["status"] == "rejected" and latest["failure"]["safe_reason_code"] == "patch_hash_conflict"
            assert latest["source_hash"] == latest["output_source_hash"] and j.checkpoint().snapshot_artifact is None
            assert result["coding"]["first_patch_round"] is None
        else:
            assert latest["status"] == "succeeded" and latest["source_hash"] != latest["output_source_hash"]
            assert j.wb.read(j.checkpoint().snapshot_artifact)["files"]["app/plans.py"]["content"] == source.replace("return True", "return True  # exact offline edit")
            assert result["coding"]["first_patch_round"] == 6
            assert result["coding"]["rounds_without_applied_patch"] == 0 and not result["coding"]["stop_required"]
    if rejected:
        assert asyncio.run(model.run_once()).state == "succeeded"
        stopped = snapshot(values)
        assert stopped["state"] == "blocked" and stopped["blocker"] == "agent_coding_stalled"
        assert stopped["usage"]["model_calls"] == 8 and stopped["usage"]["writes"] == 1
        assert len(runner.prompts) == 8
        export("oci_rejected_patch", {"before_stop": result, "after_stop": stopped, "actual_OCI": True})
    else:
        runner.responses.extend([response("run_approved_checks", {}), response(FINISH, {"reply": "request independent review"})])
        run_round(model, tool)
        assert snapshot(values)["coding"]["next_action"] == "request_finish_for_independent_review"
        assert asyncio.run(model.run_once()).state == "succeeded"
        finished = snapshot(values)
        assert finished["state"] == "finished" and len(runner.prompts) == 8
        with db.session() as session:
            j = AgentJournal(Workbench(session, config.artifact_root), task, sid)
            check = j.wb.read(next(e.payload["artifact_id"] for e in reversed(j.events()) if e.kind == "agent_v2_tool_result"))
            assert check["data"]["outcome"] == "passed" and check["output_source_hash"] == j.checkpoint().source_hash
        export("oci_sixth_patch_verify_review_gate", {"after_patch": result, "after_finish": finished,
            "actual_OCI": True, "independent_model_review": "not_run_separate_deterministic_gate_regressions"})
