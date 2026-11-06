"""Saved Plan13 input, trusted absence facts, ordered batching and bounded progress."""
import asyncio
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from app.agent_v2.contracts import MissingSourceObservation, ToolResult
from app.agent_v2.journal import AgentJournal
from app.agent_v2.loop import AgentToolWorker, prompt_for
from app.agent_v2.progress import current_progress
from app.agent_v2.tools import artifact_page
from app.provenance import content_hash
from app.workbench import Workbench
from tests.test_agent_native_tools import response
from tests.test_native_read_batches import ReadGateway, drain, inspect, setup

SAVED = json.loads((Path(__file__).parent / "fixtures/plan13_native_read_batch.json").read_text())
MISSING = SAVED["expected_missing_path"]
PATHS = [json.loads(c["function"]["arguments"])["path"] for c in SAVED["envelope"]["choices"][0]["tool_calls"]]


def saved_response():
    envelope = SAVED["envelope"]
    assert content_hash(envelope) == SAVED["response_artifact"]
    return {"choices": [{"finish_reason": c["finish_reason"], "message": {"role": c["role"],
        "tool_calls": copy.deepcopy(c["tool_calls"])}} for c in envelope["choices"]], "usage": envelope["usage"]}


def patch_response(path=MISSING):
    return response("apply_patch", {"path": path, "expected_hash": None, "old_text": "",
        "new_text": "// A new regression test fixture\n"})


def authorized_fixture(monkeypatch):
    import tests.test_workbench as original
    seed, plan_factory = original.seeded, original._plan_content
    monkeypatch.setattr(original, "_plan_content", lambda: replace(plan_factory(),
        files_to_inspect=tuple(dict.fromkeys([*plan_factory().files_to_inspect, *PATHS[:-1]])),
        files_likely_to_change=tuple(SAVED["approved_write_paths"])))

    def seed_with_absent_file(path):
        db, settings, task, content, context = seed(path)
        with db.session() as session:
            wb = Workbench(session, settings.artifact_root)
            value = wb.read(context.payload["artifact_id"])
            value["inventory"].remove(MISSING)
            value["entries"] = [e for e in value["entries"] if e["path"] != MISSING]
            updated = wb.append(task, "context_ready", {**context.payload, "artifact_id": wb.artifact(value)},
                key="fixture-absent-source")
        return db, settings, task, content, updated
    monkeypatch.setattr(original, "seeded", seed_with_absent_file)


class MissingGateway(ReadGateway):
    """Deterministic tools only; separate OCI tests run real source/filesystem checks."""
    def __init__(self):
        super().__init__()
        self.created = None

    async def preflight_read_batch(self, spec, calls, **kwargs):
        proof = await super().preflight_read_batch(spec, calls, **kwargs)
        proof["version"] = "native-read-batch-preflight-v2"
        for call, check in zip(calls, proof["checked"], strict=True):
            check["availability"] = "present"
            if call.arguments["path"] == MISSING:
                check.pop("content_hash"); check.pop("total_bytes")
                check.update(availability="not_found", reason_code="source_not_found", creation_allowed=True)
        return proof

    async def execute(self, spec, call, arguments, *, source_hash, snapshot, input_hash):
        if call.name == "apply_patch":
            self.calls.append(call)
            self.created = arguments.new_text
            output = content_hash({"base": source_hash, "created": self.created})
            return {"result": ToolResult(call_id=call.call_id, input_hash=input_hash, source_hash=source_hash,
                output_source_hash=output, status="succeeded", data={"tool": "apply_patch", "path": arguments.path}).model_dump(),
                "snapshot": {"version": "agent-workspace-snapshot-v1", "archive_hash": spec.binding.archive_hash,
                    "source_hash": output, "files": {arguments.path: {"original_hash": None, "original_content": "",
                        "content": self.created, "executable": False}}}}
        if arguments.path == MISSING:
            self.calls.append(call)
            if self.created is None:
                data = MissingSourceObservation(tool="read_file", path=MISSING, availability="not_found",
                    reason_code="source_not_found", creation_allowed=True, binding_hash=content_hash(spec.binding.model_dump()),
                    inventory_hash=source_hash, execution_backend="fixture").model_dump(exclude_none=True)
                status = "not_found"
            else:
                data = {"tool": "read_file", "execution_backend": "fixture", "path": MISSING,
                    "content_hash": hashlib.sha256(self.created.encode()).hexdigest(),
                    **artifact_page(self.created.encode(), arguments.offset, arguments.max_bytes)}
                status = "succeeded"
            return {"result": ToolResult(call_id=call.call_id, input_hash=input_hash, source_hash=source_hash,
                output_source_hash=source_hash, status=status, data=data).model_dump()}
        return await super().execute(spec, call, arguments, source_hash=source_hash, snapshot=snapshot, input_hash=input_hash)


def fixture(tmp_path, monkeypatch, *responses, gateway=None):
    authorized_fixture(monkeypatch)
    return setup(tmp_path, *responses, gateway=gateway or MissingGateway())


def test_saved_eight_calls_seven_reads_and_attested_absence_then_authorized_creation(tmp_path, monkeypatch):
    values = fixture(tmp_path, monkeypatch, saved_response(), patch_response(), response("read_file", {"path": MISSING}))
    db, settings, task, sid, model, tool, runner, gateway = values
    assert asyncio.run(model.run_once()).state == "succeeded"
    assert asyncio.run(tool.run_once()).state == "succeeded"
    for _ in range(4):
        assert asyncio.run(tool.run_once()).state == "succeeded"
    assert asyncio.run(model.run_once()) is None
    assert inspect(values)[0].state == "tools"
    restarted = AgentToolWorker(db, settings, gateway, worker_id="missing-child-restarted")
    drain(restarted)
    cp, events, progress = inspect(values)
    assert cp.state == "ready" and cp.usage["reads"] == 8
    results = [e for e in events if e.kind == "agent_v2_tool_result"]
    assert [e.payload["status"] for e in results] == ["succeeded"] * 7 + ["not_found"]
    assert [e.payload["provider_call_id"] for e in results] == [c["id"] for c in SAVED["envelope"]["choices"][0]["tool_calls"]]
    assert [len(e.payload["call_ids"]) for e in events if e.kind == "agent_v2_read_sub_batch_completed"] == [4, 4]
    assert len({c.call_id for c in gateway.calls}) == 8 and len(runner.prompts) == 1
    assert progress.steps[3].status == "pending" and progress.phase == "investigating"
    with db.session() as session:
        journal = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        prompt = prompt_for(journal)
        absence = prompt["feedback"][-1]
        assert absence["status"] == "not_found" and absence["failure"] is None
        assert not ({"content_hash", "text", "total_bytes", "next_offset"} & absence["data"].keys())
        assert prompt["source_files"]["approved_new_paths_currently_absent"] == [MISSING]
        assert MISSING not in prompt["source_files"]["existing_relevant_paths"]
        assert len(prompt["feedback"]) == 8
    assert asyncio.run(model.run_once()).state == "succeeded"
    assert runner.prompts[-1]["feedback"][-1]["status"] == "not_found"
    drain(restarted)
    cp, _, progress = inspect(values)
    assert gateway.created and cp.usage["writes"] == 1 and cp.usage["reads"] == 8
    assert progress.steps[3].status == "completed" and progress.steps[4].status == "pending"
    assert asyncio.run(model.run_once()).state == "succeeded"
    drain(restarted)
    cp, events, _ = inspect(values)
    assert cp.usage["reads"] == 9 and events[-2].kind != "agent_v2_tool_reused"
    with db.session() as session:
        journal = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        prompt = prompt_for(journal)
        assert prompt["feedback"][-1]["status"] == "succeeded"
        assert MISSING not in prompt["source_files"]["approved_new_paths_currently_absent"]
        assert MISSING in prompt["source_files"]["existing_relevant_paths"]


def test_repeated_missing_evidence_is_reused_and_does_not_complete_coding(tmp_path, monkeypatch):
    read = response("read_file", {"path": MISSING})
    values = fixture(tmp_path, monkeypatch, *[copy.deepcopy(read) for _ in range(4)])
    for _ in range(4):
        assert asyncio.run(values[4].run_once()).state == "succeeded"
        drain(values[5])
    cp, events, progress = inspect(values)
    assert cp.state == "blocked" and progress.blocker == "agent_no_progress"
    assert len(values[-1].calls) == 1 and cp.usage["reads"] == 4
    assert progress.steps[3].status == "pending"
    assert len([e for e in events if e.kind == "agent_v2_tool_reused"]) == 3


@pytest.mark.parametrize("bad", ["forged_content", "wrong_binding", "unapproved_path", "wrong_expected_hash", "present_preflight"])
def test_forged_missing_observation_or_preflight_cannot_relax_security(tmp_path, monkeypatch, bad):
    class Forged(MissingGateway):
        async def preflight_read_batch(self, spec, calls, **kwargs):
            proof = await super().preflight_read_batch(spec, calls, **kwargs)
            if bad == "unapproved_path":
                proof["checked"][0] = {**{k: proof["checked"][0][k] for k in ("path", "offset", "max_bytes")},
                    "availability": "not_found", "reason_code": "source_not_found", "creation_allowed": True}
                # The first path is approved too. A read-only exploration path isn't.
                proof["checked"][5] = {**{k: proof["checked"][5][k] for k in ("path", "offset", "max_bytes")},
                    "availability": "not_found", "reason_code": "source_not_found", "creation_allowed": True}
            if bad == "present_preflight":
                proof["checked"][-1] = {**{k: proof["checked"][-1][k] for k in ("path", "offset", "max_bytes")},
                    "availability": "present", "content_hash": "a" * 64, "total_bytes": 1}
            return proof

        async def execute(self, *args, **kwargs):
            value = await super().execute(*args, **kwargs)
            if value["result"]["status"] == "not_found":
                if bad == "forged_content": value["result"]["data"]["text"] = "invented"
                if bad == "wrong_binding": value["result"]["data"]["binding_hash"] = "b" * 64
            return value
    raw = saved_response()
    if bad == "wrong_expected_hash":
        raw["choices"][0]["message"]["tool_calls"][-1]["function"]["arguments"] = json.dumps({"path": MISSING, "expected_hash": "a" * 64})
    values = fixture(tmp_path, monkeypatch, raw, gateway=Forged())
    asyncio.run(values[4].run_once()); drain(values[5])
    cp, events, _ = inspect(values)
    assert cp.state == "blocked" and cp.usage["reads"] == 8
    assert not any(e.kind == "agent_v2_read_batch_completed" for e in events)
    if bad in {"unapproved_path", "wrong_expected_hash"}:
        assert not values[-1].calls


def test_follow_up_creation_cannot_escape_approved_write_paths(tmp_path, monkeypatch):
    values = fixture(tmp_path, monkeypatch, saved_response(), patch_response("not-approved.ts"))
    asyncio.run(values[4].run_once()); drain(values[5])
    asyncio.run(values[4].run_once()); drain(values[5])
    assert values[-1].created is None and inspect(values)[0].usage.get("writes", 0) == 0


def test_actual_process_crash_between_children_with_a_later_missing_file(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    counter = tmp_path / "executed-ids.jsonl"
    class Meter(MissingGateway):
        async def execute(self, spec, call, *args, **kwargs):
            with counter.open("a") as target:
                target.write(call.call_id + "\n"); target.flush(); os.fsync(target.fileno())
            return await super().execute(spec, call, *args, **kwargs)
    values = fixture(tmp_path, monkeypatch, saved_response(), gateway=Meter())
    asyncio.run(values[4].run_once())
    for _ in range(3): asyncio.run(values[5].run_once())
    settings = values[1]
    config = {"database_url": settings.database_url, "artifact_root": settings.artifact_root,
        "sandbox_job_spec_signing_key": settings.sandbox_job_spec_signing_key.hex(),
        "agent_runtime_v2_enabled": True, "counter": str(counter)}
    program = """import asyncio,json,os,sys
from pathlib import Path
from app.config import Settings
from app.database import Database
from app.agent_v2.loop import AgentToolWorker
from tests.test_missing_source_observations import MissingGateway
config=json.load(sys.stdin);counter=Path(config.pop('counter'));config['sandbox_job_spec_signing_key']=bytes.fromhex(config['sandbox_job_spec_signing_key']);settings=Settings(**config);db=Database(settings.database_url)
class Meter(MissingGateway):
 async def execute(self,spec,call,*args,**kwargs):
  with counter.open('a') as target:target.write(call.call_id+'\\n');target.flush();os.fsync(target.fileno())
  return await super().execute(spec,call,*args,**kwargs)
worker=AgentToolWorker(db,settings,Meter(),worker_id='missing-source-process')
for _ in range(2):assert asyncio.run(worker.run_once()).state=='succeeded'
os._exit(23)
"""
    child = subprocess.run([sys.executable, "-c", program], input=json.dumps(config), text=True, capture_output=True,
        env={k: os.environ[k] for k in ("PATH", "LANG") if k in os.environ}, timeout=40)
    assert child.returncode == 23, child.stderr
    drain(values[5])
    ids = counter.read_text().splitlines()
    assert len(ids) == len(set(ids)) == 8
    cp, events, _ = inspect(values)
    assert cp.state == "ready" and cp.usage["reads"] == 8
    assert [e.payload["status"] for e in events if e.kind == "agent_v2_tool_result"] == ["succeeded"] * 7 + ["not_found"]


def test_forward_migration_keeps_old_journal_and_rejects_forged_absence(tmp_path, monkeypatch):
    from app.migrations import versions
    from sqlalchemy.exc import IntegrityError
    with monkeypatch.context() as patch:
        patch.setattr(versions, "MIGRATIONS", tuple(m for m in versions.MIGRATIONS if m.revision < "0045_source_file_observations"))
        values = fixture(tmp_path, patch, saved_response())
    db, settings, task, sid = values[:4]
    before = [e.record_hash for e in inspect(values)[1]]
    assert db.create_schema().applied == ("0045_source_file_observations",)
    assert [e.record_hash for e in inspect(values)[1]] == before
    with db.session() as session:
        journal = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        aid = journal.wb.artifact({"forged": True})
        with pytest.raises(IntegrityError, match="Invalid approved missing source receipt"):
            journal.append("tool_result", {"name": "apply_patch", "call_id": "forged", "status": "not_found",
                "failure": None, "request_arguments": {"path": MISSING, "expected_hash": None}, "artifact_id": aid}, key="forged-missing")
        session.rollback()
    assert asyncio.run(values[4].run_once()).state == "succeeded"
    drain(values[5])
    assert inspect(values)[0].state == "ready"
