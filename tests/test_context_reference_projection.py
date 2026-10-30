"""Prompt hash sharing preserves full checkpoint validation and exact tool pages."""
import hashlib
import json

import pytest

from app.context_compaction import ContextRead, compact_prompt, read_context
from app.planner import PlanningService
from app.provenance import canonical_json
from app.workbench import WorkbenchError
from tests.test_context_compaction import large_fixture, packet


@pytest.mark.parametrize("paged", [False, True])
def test_reference_projection_preserves_hashes_reads_and_legacy_wire(tmp_path, paged):
    db, settings, task, _, _ = large_fixture(tmp_path)
    source = "# EXACT_SOURCE\n" + "value = 'original source'\n" * 300
    entries = [{"path":f"src/module_{i}.py", "content":source,
        "prior_hash":hashlib.sha256(source.encode()).hexdigest(), "executable":False} for i in range(24)]
    pages = [{"source_path":"src/current.py", "source_hash":"a"*64,
        "text":"EXACT_CURRENT_READ " + "x"*8000, "partial":True} for _ in range(3)]
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        payload = packet(wb, task)
        payload.update(evidence={"entries":entries,"inventory":[e["path"] for e in entries]},
            context_reads=pages, rules=["Frozen rule " + "r"*35000], extra_log="Source-labelled retained log " + "z"*5000)
        original_payload = canonical_json(payload)
        prompt, cp = compact_prompt(wb, task, payload, scope="planning", page_source_files=paged,
            target_bytes=48000, trigger_bytes=48000)
        actual = json.loads(prompt)
        checkpoint = wb.read(cp["artifact_id"])
        manifest = {r["reference_id"]:r for r in checkpoint["references"]}
        projected = {r["reference_id"]:r for r in actual["context_management"]["references"]}
        assert manifest.keys() == projected.keys()
        assert actual["context_reads"] == pages and actual["rules"] == payload["rules"]
        assert actual["request"] == payload["request"] and canonical_json(payload) == original_payload
        shared = 0
        for entry in actual["evidence"]["entries"]:
            ref = entry.get("context_reference")
            if not ref:
                continue
            assert manifest[ref]["source_hash"] == entry["prior_hash"]
            if paged:
                assert "source_hash" not in projected[ref]
                assert {**projected[ref],"source_hash":entry["prior_hash"]} == manifest[ref]
                shared += 1
            else:
                assert projected[ref] == manifest[ref]
        assert shared == (24 if paged else 0)
        if paged:
            assert len(prompt.encode()) < 96000
        else:
            assert all("source_hash" in ref for ref in projected.values())
        extra = next(r for r in checkpoint["references"] if r["label"]=="extra_log")
        assert projected[extra["reference_id"]] == extra
        target = next(r for r in checkpoint["references"] if r["label"]=="repository file: src/module_0.py")
        page = read_context(wb, task, cp["artifact_id"], [ContextRead(reference_id=target["reference_id"],max_bytes=16000)])[0]
        assert json.loads(page["text"])["content"] == source
        assert page["source_hash"] == entries[0]["prior_hash"] and page["source_complete"]
        with pytest.raises(WorkbenchError) as caught:
            read_context(wb, task, cp["artifact_id"], [ContextRead(reference_id="f"*64)])
        assert caught.value.code == "context_reference_invalid"
        assert wb.latest(task,"execution_authorized") is None
    db.close()
