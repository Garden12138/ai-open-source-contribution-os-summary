"""Deterministic source-to-patch substitute, using real offline OCI file edits."""
import asyncio
import os

import pytest

from app.agent_v2.contracts import ToolCall
from app.agent_v2.tools import RestrictedToolRegistry
from tests.test_native_read_batches_oci import actual

pytestmark = pytest.mark.skipif(os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1",
    reason="Requires real credential-free OCI; never a real model success claim")


def test_source_evidence_drives_deterministic_patch_and_exact_oci_readback(tmp_path):
    spec, gateway, _, _ = actual(tmp_path)
    registry = RestrictedToolRegistry()
    async def scenario():
        read = ToolCall(call_id="substitute-source", name="read_file", arguments={"path": "calc.py"})
        first = await gateway.execute(spec, read, registry.validate(read, spec),
            source_hash=spec.binding.archive_hash, snapshot=None, input_hash="a" * 64)
        receipt = first["result"]
        assert receipt["status"] == "succeeded"
        data = receipt["data"]
        assert data["execution_backend"] == "docker" and data["text"]
        # The substitute uses only the actual observed page/hash, never a
        # pre-scripted file body or an expected_hash invented by the platform.
        patch = ToolCall(call_id="substitute-patch", name="apply_patch", arguments={
            "path": data["path"], "expected_hash": data["content_hash"],
            "old_text": "", "new_text": "# deterministic offline fixture edit\n"})
        # Empty old_text is not legal for an existing file. Use the complete
        # observed prefix, whose replacement must match exactly once.
        patch = patch.model_copy(update={"arguments": {**patch.arguments,
            "old_text": data["text"], "new_text": "# deterministic offline fixture edit\n" + data["text"]}})
        # The repeating fixture makes a partial prefix ambiguous; recover the
        # entire file through the existing bounded read tool first.
        read = read.model_copy(update={"arguments": {"path": "calc.py", "max_bytes": 16000}})
        full = await gateway.execute(spec, read, registry.validate(read, spec),
            source_hash=receipt["output_source_hash"], snapshot=None, input_hash="b" * 64)
        data = full["result"]["data"]
        assert data["next_offset"] is None
        patch = patch.model_copy(update={"arguments": {**patch.arguments,
            "old_text": data["text"], "new_text": "# deterministic offline fixture edit\n" + data["text"]}})
        edited = await gateway.execute(spec, patch, registry.validate(patch, spec),
            source_hash=full["result"]["output_source_hash"], snapshot=None, input_hash="c" * 64)
        assert edited["result"]["status"] == "succeeded"
        assert edited["result"]["output_source_hash"] != full["result"]["output_source_hash"]
        latest = await gateway.execute(spec, read, registry.validate(read, spec),
            source_hash=edited["result"]["output_source_hash"], snapshot=edited["snapshot"], input_hash="d" * 64)
        assert latest["result"]["data"]["text"] == patch.arguments["new_text"]
        assert edited["snapshot"]["files"]["calc.py"]["content"] == patch.arguments["new_text"]
    asyncio.run(scenario())
