"""Actual OCI source absence, path security and the frozen Plan13 archive."""
import asyncio
import hashlib
import os
from pathlib import Path

import pytest

from app.agent_v2.contracts import ToolCall
from app.agent_v2.native_tools import native_model_transport
from app.agent_v2.read_batches import ReadBatchPreflightV2
from app.agent_v2.tools import RestrictedToolRegistry
from app.archives import RepositoryArchiveStore
from app.provenance import content_hash
from app.sandbox_worker.agent_tools import DockerAgentToolGateway
from app.workbench import WorkbenchError
from tests.test_agent_v2_tools import tool_spec
from tests.test_native_read_batches_oci import actual
from tests.test_missing_source_observations import MISSING, SAVED

pytestmark = pytest.mark.skipif(os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1",
    reason="Requires real OCI engine, fixed image and frozen archive; never a fake pass")


def frozen_gateway(tmp_path):
    archive = Path(os.environ["CONTRIBOS_PLAN13_ARCHIVE_PATH"])
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == "d269dc46fd82a9ae5d443b95aa389bb6637856f28f5e5cb7da0d0392384b2cdb"
    root = str(tmp_path / "frozen-artifacts")
    digest = RepositoryArchiveStore(root).put_file(archive)
    spec = tool_spec(digest, os.environ["CONTRIBOS_SANDBOX_RUNNER_IMAGE"])
    spec = spec.model_copy(update={"binding": spec.binding.model_copy(update={
        "base_sha": "3489e17c48985b74fdb42cde122376b1eb12d8a7"}),
        "write_paths": SAVED["approved_write_paths"], "model_transport": native_model_transport()})
    environment = {k: os.environ[k] for k in ("HOME", "PATH", "TMPDIR", "DOCKER_CONTEXT", "DOCKER_HOST") if k in os.environ}
    gateway = DockerAgentToolGateway(artifact_root=root, docker_environment=environment,
        repository_for_attempt=lambda _: "patoles/agent-flow")
    import json
    calls = [ToolCall(call_id=c["id"], name=c["function"]["name"], arguments=json.loads(c["function"]["arguments"]))
        for c in SAVED["envelope"]["choices"][0]["tool_calls"]]
    return spec, gateway, calls


def test_real_frozen_plan13_eight_reads_and_authorized_new_file_creation(tmp_path):
    spec, gateway, calls = frozen_gateway(tmp_path)
    registry = RestrictedToolRegistry()
    async def scenario():
        proof = await gateway.preflight_read_batch(spec, calls, source_hash=spec.binding.archive_hash,
            snapshot=None, input_hash="a" * 64)
        ReadBatchPreflightV2.model_validate(proof)
        assert proof["status"] == "succeeded" and proof["source_hash"] == "7f02d8377a922ec5473bd68159817d058064e8a2892f65ef33ba17362bf9265c"
        assert [c["availability"] for c in proof["checked"]] == ["present"] * 7 + ["not_found"]
        results = []
        for start in range(0, 8, 4):
            for call in calls[start:start + 4]:
                value = await gateway.execute(spec, call, registry.validate(call, spec), source_hash=proof["source_hash"],
                    snapshot=None, input_hash=content_hash(call.model_dump()))
                results.append(value["result"])
        assert [r["call_id"] for r in results] == [c.call_id for c in calls]
        assert [r["status"] for r in results] == ["succeeded"] * 7 + ["not_found"]
        absent = results[-1]
        assert not ({"text", "content_hash", "total_bytes", "next_offset"} & absent["data"].keys())
        assert absent["failure"] is None and absent["data"]["inventory_hash"] == proof["source_hash"]
        for result in results[:-1]:
            data = result["data"]; end = data["offset"] + len(data["text"].encode())
            assert data["next_offset"] == (end if end < data["total_bytes"] else None)
        patch = ToolCall(call_id="simulated-next-model-creation", name="apply_patch", arguments={
            "path": MISSING, "expected_hash": None, "old_text": "", "new_text": "// offline OCI regression fixture\n"})
        created = await gateway.execute(spec, patch, registry.validate(patch, spec), source_hash=proof["source_hash"],
            snapshot=None, input_hash=content_hash(patch.model_dump()))
        assert created["result"]["status"] == "succeeded" and created["snapshot"]["files"][MISSING]["original_hash"] is None
        read = calls[-1]
        current = await gateway.execute(spec, read, registry.validate(read, spec), source_hash=created["result"]["output_source_hash"],
            snapshot=created["snapshot"], input_hash="c" * 64)
        assert current["result"]["status"] == "succeeded" and current["result"]["data"]["text"] == patch.arguments["new_text"]
        outside = patch.model_copy(update={"arguments": {**patch.arguments, "path": "outside.ts"}})
        with pytest.raises(WorkbenchError):
            registry.validate(outside, spec)
        output = os.environ.get("CONTRIBOS_MISSING_SOURCE_EVIDENCE")
        if output:
            import json
            evidence = {"classification": "real_saved_Plan13_envelope_real_frozen_archive_real_OCI_synthetic_new_authority",
                "historical_approval_reused": False, "provider_requests": 0,
                "original_response_artifact": SAVED["response_artifact"], "archive_hash": spec.binding.archive_hash,
                "binding_hash": content_hash(spec.binding.model_dump()), "preflight": proof,
                "child_sizes": [4, 4], "ordered_calls": [c.model_dump() for c in calls],
                "ordered_results": results, "simulated_next_model_patch": created,
                "read_after_creation": current, "new_Plan_created": False}
            from app.security import ensure_no_sensitive_data
            ensure_no_sensitive_data(evidence, context="frozen OCI replay evidence")
            evidence["report_hash"] = content_hash(evidence)
            with Path(output).open("x") as target:
                target.write(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    asyncio.run(scenario())


@pytest.mark.parametrize("bad", ["unapproved_missing", "symlink", "symlink_parent", "escape", "hash", "missing_with_hash", "permission_or_unknown"])
def test_real_oci_preflight_security_failures_remain_whole_batch_rejections(tmp_path, bad):
    spec, gateway, calls, _ = actual(tmp_path)
    args = {"path": "missing.py" if bad in {"unapproved_missing", "missing_with_hash"} else
        "linked" if bad == "symlink" else "linked/missing.ts" if bad == "symlink_parent" else "../outside" if bad == "escape" else "calc.py"}
    if bad in {"hash", "missing_with_hash"}: args["expected_hash"] = "b" * 64
    if bad == "missing_with_hash": spec = spec.model_copy(update={"write_paths": ["calc.py", "missing.py"]})
    if bad == "symlink_parent": spec = spec.model_copy(update={"write_paths": ["calc.py", "linked/missing.ts"]})
    calls[-1] = calls[-1].model_copy(update={"arguments": args})
    if bad == "permission_or_unknown":
        # Trusted OCI script injects an actual OSError before read prevalidation.
        # Application source is never executed and the error cannot become absence.
        from app.sandbox_worker import agent_tools
        original = agent_tools.SOURCE_TOOL_SCRIPT
        agent_tools.SOURCE_TOOL_SCRIPT = original.replace("checked=[]", "raise OSError('unknown OCI fixture error'); checked=[]")
    async def scenario():
        if bad == "escape":
            with pytest.raises(ValueError): await gateway.preflight_read_batch(spec, calls, source_hash=spec.binding.archive_hash, snapshot=None, input_hash="a" * 64)
            return
        proof = await gateway.preflight_read_batch(spec, calls, source_hash=spec.binding.archive_hash, snapshot=None, input_hash="a" * 64)
        assert proof["status"] == "rejected" and proof["checked"] == []
        expected = {"unapproved_missing": "source_not_found", "symlink": "symlink_denied", "symlink_parent": "symlink_denied", "hash": "source_hash_conflict",
            "missing_with_hash": "source_hash_conflict", "permission_or_unknown": "source_unavailable"}
        assert proof["failure_code"] == expected[bad]
        assert "text" not in proof
    try:
        asyncio.run(scenario())
    finally:
        if bad == "permission_or_unknown": agent_tools.SOURCE_TOOL_SCRIPT = original
