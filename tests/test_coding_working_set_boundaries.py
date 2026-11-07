"""Projection stress cases; no model, job, approval or target repository execution."""
from hashlib import sha256
from types import SimpleNamespace

import pytest

from app.agent_v2.coding_context import observe
from app.agent_v2.contracts import ToolCall, ToolResult
from app.agent_v2.model_receipts import response_evidence
from app.agent_v2.native_tools import native_input_bound, native_model_transport, project_native_prompt
from app.agent_v2.tools import RestrictedToolRegistry, artifact_page
from app.provenance import canonical_json, content_hash
from tests.test_agent_native_tools import response
from tests.test_agent_v2_tools import tool_spec
from tests.test_coding_stop_boundaries import export


def projection_fixture(latest="extension", *, modules=None, files_per_module=4):
    older = "web" if latest == "extension" else "extension"
    paths = [f"{module}/file{i}.ts" for module in (modules or (older, latest)) for i in range(files_per_module)]
    spec = tool_spec().model_copy(update={"write_paths": paths, "model_transport": native_model_transport()})
    objects, raw, events = {}, {}, []
    def save(value):
        aid = content_hash(value); objects[aid] = value; raw[aid] = canonical_json(value).encode(); return aid
    def receipt(tool, data):
        value = ToolResult(call_id="call-" + str(len(events)), input_hash="a" * 64, status="succeeded",
            source_hash=spec.binding.archive_hash, output_source_hash=spec.binding.archive_hash,
            data={"tool": tool, **data}).model_dump()
        aid = save(value)
        events.append(SimpleNamespace(kind="agent_v2_tool_result", sequence=len(events) + 1,
            actor_id="orchestrator", job_id="tool-" + str(len(events)), payload={"name": tool, "artifact_id": aid}))
        return {**value, "receipt_artifact": aid}
    originals = []
    for path in paths:
        body = ("// " + path + " exact observed source\n") * 300
        page = body.encode()[:4096].decode()
        page_id = sha256(page.encode()).hexdigest(); raw[page_id] = page.encode()
        originals.append(receipt("read_file", {"execution_backend": "docker", "path": path, "text": page,
            "content_hash": sha256(body.encode()).hexdigest(), "source_page_artifact": page_id,
            "offset": 0, "next_offset": len(page.encode()), "total_bytes": len(body.encode())}))
    task = {"binding_hash": content_hash(spec.binding.model_dump()), "approved_plan": {
        "goal": spec.goal, "acceptance_criteria": ["Preserve both modules and exact provenance"],
        "implementation_steps": ["Inspect exact code and implement within scope"]}, "issue": {"title": "Fixture"}}
    task_id = save(task)
    j = SimpleNamespace(events=lambda: events, spec=lambda: spec,
        checkpoint=lambda: SimpleNamespace(source_hash=spec.binding.archive_hash),
        wb=SimpleNamespace(read=lambda aid: objects[aid], artifacts=SimpleNamespace(read_bytes=lambda aid: raw[aid])))
    def render(feedback):
        coding, pages = observe(j)
        index = [{"tool": objects[e.payload["artifact_id"]]["data"]["tool"],
            "path": objects[e.payload["artifact_id"]]["data"].get("path"), "artifact_id": e.payload["artifact_id"]}
            for e in events if e.kind == "agent_v2_tool_result"]
        frame = {"binding": spec.binding.model_dump(), "goal": spec.goal, "write_paths": paths,
            "approved_checks": spec.commands, "history_index": index, "feedback": feedback,
            "implementation_progress": {"coding": coding}, "task_context_artifact": task_id,
            "approved_task_input": {"partial": True, "artifact_id": task_id}}
        result = project_native_prompt(frame, spec, task_input=task, source_pages=pages,
            history_artifact=save({"rows": index}), journal=j)
        assert native_input_bound(result) <= 32000
        ordinal = len(events)
        job = "model-" + str(ordinal); prompt_id = save(result)
        events.append(SimpleNamespace(kind="agent_v2_model_started", sequence=ordinal + 1,
            actor_id="orchestrator", job_id=job, payload={"prompt_artifact": prompt_id}))
        reply = response_evidence(response("list_paths", {}), request_id=job,
            binding_hash=content_hash(spec.binding.model_dump()))
        events.append(SimpleNamespace(kind="agent_v2_model_response", sequence=ordinal + 2,
            actor_id="orchestrator", job_id=job, payload={"artifact_id": save(reply),
                "prompt_artifact": prompt_id, "binding_hash": reply["binding_hash"]}))
        return result
    return j, originals, render, receipt, raw


def test_six_renamed_modules_rotate_four_slots_deterministically_across_recovery():
    modules = ["packages/alpha", "packages/bravo", "packages/charlie", "packages/delta", "packages/echo", "packages/foxtrot"]
    j, originals, render, _, _ = projection_fixture(modules=modules, files_per_module=1)
    frames = [render(originals) for _ in range(3)]
    selected = [[p["module"] for p in f["source_working_set"]] for f in frames]
    assert all(len(set(row)) == 4 for row in selected)
    assert set(selected[0] + selected[1]) == set(modules)
    from app.agent_v2.coding_context import select_working_set
    pages = observe(j)[1]
    # Reconstructed Journal facade has no scheduler state or cached selection.
    recovered = SimpleNamespace(**vars(j))
    assert select_working_set(pages, j.spec().write_paths, [], j) == select_working_set(pages, j.spec().write_paths, [], recovered)
    export("v11_six_module_rotation", {"selected_modules": selected, "recovery_deterministic": True})


def test_single_module_fills_four_slots_and_starts_at_unseen_source():
    j, originals, render, _, _ = projection_fixture(modules=["engine"], files_per_module=5)
    first, second = render(originals), render(originals)
    assert len(first["source_working_set"]) == len(second["source_working_set"]) == 4
    assert len({p["module"] for p in first["source_working_set"]}) == 1
    first_ranges = {p["path"]: p["offset"] + len(p["text"].encode()) for p in first["source_working_set"]}
    assert all(p["offset"] >= first_ranges[p["path"]] for p in second["source_working_set"] if p["path"] in first_ranges)


def test_authorized_observed_paths_displace_unrelated_pages_without_inventing_reads():
    from app.agent_v2.coding_context import select_working_set
    j, _, _, _, _ = projection_fixture(modules=["engine", "other"], files_per_module=4)
    pages = observe(j)[1]
    allowed = [p["path"] for p in pages if p["path"].startswith("engine/")]
    chosen = select_working_set(pages, allowed, [], j)
    assert len(chosen) == 4 and {p["path"] for p in chosen} == set(allowed)
    assert all(p["priority"] == 0 for p in chosen)
    assert {p["receipt_artifact"] for p in chosen} <= {p["receipt_artifact"] for p in pages}


@pytest.mark.parametrize("latest", ["extension", "web"])
def test_fairness_prevents_persistent_exclusion_of_either_authorized_module(latest):
    journal, originals, render, _, _ = projection_fixture(latest)
    frames = [render(originals) for _ in range(4)]
    assert all({p["path"].split("/")[0] for p in f["source_working_set"]} == {"web", "extension"} for f in frames)
    metrics = observe(journal)[0]
    older = "web" if latest == "extension" else "extension"
    assert any(row["delivered"] for row in metrics["source_index"] if row["path"].startswith(older + "/"))
    export("selection_latest_" + latest, {"latest_read_module": latest,
        "selected_paths_per_frame": [[p["path"] for p in f["source_working_set"]] for f in frames],
        "module_fairness_guaranteed": True, "coding": metrics})


def test_excluded_module_receipt_and_raw_source_pages_are_recoverable():
    journal, originals, render, receipt, raw = projection_fixture()
    frame = render(originals)
    missing = originals[0]
    assert missing["data"]["path"].startswith("web/")
    assert missing["receipt_artifact"] in {r["artifact_id"] for r in frame["history_index"]}
    metadata = receipt("read_artifact", {"artifact_id": missing["receipt_artifact"],
        **artifact_page(raw[missing["receipt_artifact"]], 0, 16000)})
    recovered_metadata = render([metadata])
    page_id = missing["data"]["source_page_artifact"]
    assert page_id in recovered_metadata["feedback"][0]["data"]["text"]
    full_page = raw[page_id]
    calls = [receipt("read_artifact", {"artifact_id": page_id,
        **artifact_page(full_page, start, 1024)}) for start in range(0, len(full_page), 1024)]
    recovered = render(calls)
    pieces = [row["data"] for row in recovered["feedback"]]
    assert len(pieces) == 4 and b"".join(p["text"].encode() for p in pieces) == full_page
    assert [p["offset"] for p in pieces] == [0, 1024, 2048, 3072]
    assert pieces[-1]["next_offset"] is None and sha256(full_page).hexdigest() == page_id
    assert observe(journal)[0]["source_index"][4]["path"] == "web/file0.ts"
    web = next(r for r in observe(journal)[0]["source_index"] if r["path"] == "web/file0.ts")
    assert web["delivered"] == [[0, 4096]]
    # A complete raw page is still not the complete file. Its remaining bytes
    # require an ordinary budgeted source read, never fabricated content.
    args = RestrictedToolRegistry().validate(ToolCall(call_id="tail", name="read_file", arguments={
        "path": missing["data"]["path"], "offset": missing["data"]["next_offset"],
        "expected_hash": missing["data"]["content_hash"]}), journal.spec())
    assert args.offset == 4096 and missing["data"]["total_bytes"] > args.offset
    export("module_page_recovery", {"path": missing["data"]["path"], "recovered_raw_page_bytes": 4096,
        "full_file_recovered": False, "next_source_offset": args.offset,
        "selected_paths_still_recent_module": [p["path"] for p in recovered["source_working_set"]],
        "input_bound": native_input_bound(recovered), "coding": observe(journal)[0]})
