"""Delivery witnesses, not dispatch intents, establish observed read warnings."""
import copy
from hashlib import sha256
from types import SimpleNamespace

import pytest

from app.agent_v2.contracts import ReadFile, ToolResult
from app.agent_v2.coding_context import observe
from app.agent_v2.model_receipts import response_evidence
from app.agent_v2.read_progress import ReadProgressNote, _key, source_read_progress
from app.provenance import content_hash
from app.workbench import WorkbenchError
from tests.test_agent_native_tools import response
from tests.test_agent_v2_tools import tool_spec


def delivery_journal(*, responded=True, placement="both"):
    spec = tool_spec().model_copy(update={"model_transport": __import__(
        "app.agent_v2.native_tools", fromlist=["native_model_transport"]).native_model_transport()})
    source, text, args = spec.binding.archive_hash, "observed source\n", ReadFile(path="calc.py", max_bytes=128)
    file_hash = sha256(text.encode()).hexdigest()
    objects = {}
    def save(value):
        aid = content_hash(value); objects[aid] = value; return aid
    events = []
    for i in range(2):
        note = ReadProgressNote(range_key=_key(source, file_hash, args), max_bytes=128,
            prior_same_range_reads=i, prior_receipt=events[-1].payload["artifact_id"] if events else None,
            warning_receipts_seen=[], visible_bytes=0, visible_next_offset=None)
        receipt = ToolResult(call_id="read-" + str(i), input_hash="b" * 64,
            source_hash=source, output_source_hash=source, status="succeeded", data={
                "tool": "read_file", "execution_backend": "docker", "path": "calc.py", "text": text,
                "offset": 0, "next_offset": None, "total_bytes": len(text.encode()), "content_hash": file_hash,
                "source_page_artifact": file_hash, "source_read_progress": note.model_dump()}).model_dump()
        aid = save(receipt)
        events.append(SimpleNamespace(kind="agent_v2_tool_result", sequence=i + 1, actor_id="orchestrator",
            payload={"name": "read_file", "artifact_id": aid}, job_id="tool-" + str(i)))
    prompt = {"binding": spec.binding.model_dump(), "task_context_artifact": "c" * 64,
        "feedback": [{**receipt, "receipt_artifact": aid}] if placement in {"feedback", "both"} else [],
        "source_working_set": [{"receipt_artifact": aid, "path": "calc.py", "offset": 0, "text": text,
            "next_offset": None, "total_bytes": len(text.encode()), "output_source_hash": source,
            "source_read_progress": {k: v for k, v in note.model_dump().items() if k != "instruction"}}]
            if placement in {"working_set", "both"} else []}
    start = SimpleNamespace(kind="agent_v2_model_started", sequence=3, actor_id="orchestrator", job_id="model-3",
        payload={"prompt_artifact": save(prompt)})
    events.append(start)
    if responded:
        raw = response_evidence(response("list_paths", {}), request_id=start.job_id,
            binding_hash=content_hash(spec.binding.model_dump()))
        events.append(SimpleNamespace(kind="agent_v2_model_response", sequence=4, actor_id="orchestrator",
            job_id=start.job_id, payload={"artifact_id": save(raw), "prompt_artifact": start.payload["prompt_artifact"],
                "binding_hash": raw["binding_hash"]}))
    def read(aid):
        value = objects[aid]; assert content_hash(value) == aid; return value
    journal = SimpleNamespace(spec=lambda: spec, events=lambda: events,
        checkpoint=lambda: SimpleNamespace(source_hash=source), wb=SimpleNamespace(read=read,
            artifacts=SimpleNamespace(read_bytes=lambda _: text.encode())))
    return journal, args, objects


@pytest.mark.parametrize("placement", ["feedback", "working_set", "both"])
def test_dispatch_without_response_does_not_claim_warning_delivery(placement):
    journal, args, _ = delivery_journal(responded=False, placement=placement)
    note = source_read_progress(journal, args).note
    assert note.warning_receipts_seen == []
    assert note.visible_bytes == 0
    assert observe(journal)[0]["delivered_source_bytes"] == 0


@pytest.mark.parametrize("placement", ["feedback", "working_set", "both"])
def test_matching_delivery_deduplicates_warning_across_both_projections(placement):
    journal, args, _ = delivery_journal(placement=placement)
    note = source_read_progress(journal, args).note
    assert len(note.warning_receipts_seen) == 1
    assert note.visible_bytes == len("observed source\n".encode())
    # A reconstructed reader has no private counter to restore or double-count.
    restarted = SimpleNamespace(**journal.__dict__)
    assert source_read_progress(restarted, args).note == note
    assert observe(restarted)[0]["delivered_source_bytes"] == note.visible_bytes


@pytest.mark.parametrize("fault", ["response_actor", "start_actor", "prompt_reference", "event_binding",
    "raw_binding", "raw_request", "prompt_binding", "before_start", "duplicate_response", "response_job"])
def test_identity_mismatch_is_fail_closed_in_both_consumers(fault):
    journal, args, objects = delivery_journal()
    events = journal.events(); start, reply = events[-2:]
    if fault == "response_actor": reply.actor_id = "model"
    elif fault == "start_actor": start.actor_id = "model"
    elif fault == "prompt_reference": reply.payload["prompt_artifact"] = "f" * 64
    elif fault == "event_binding": reply.payload["binding_hash"] = "f" * 64
    elif fault in {"raw_binding", "raw_request"}:
        raw = copy.deepcopy(objects[reply.payload["artifact_id"]])
        raw["binding_hash" if fault == "raw_binding" else "request_id"] = "f" * 64
        aid = content_hash(raw); objects[aid] = raw; reply.payload["artifact_id"] = aid
    elif fault == "prompt_binding":
        prompt = copy.deepcopy(objects[start.payload["prompt_artifact"]]); prompt["binding"]["plan_id"] = "other-plan"
        aid = content_hash(prompt); objects[aid] = prompt
        start.payload["prompt_artifact"] = reply.payload["prompt_artifact"] = aid
        raw = copy.deepcopy(objects[reply.payload["artifact_id"]]); raw["binding_hash"] = content_hash(prompt["binding"])
        rid = content_hash(raw); objects[rid] = raw; reply.payload.update(artifact_id=rid, binding_hash=raw["binding_hash"])
    elif fault == "before_start": reply.sequence = start.sequence - 1
    elif fault == "duplicate_response": events.append(copy.deepcopy(reply))
    elif fault == "response_job": reply.job_id = "unrelated-job"
    for reader in (lambda: source_read_progress(journal, args), lambda: observe(journal)):
        with pytest.raises(WorkbenchError) as error:
            reader()
        assert error.value.code == "agent_model_delivery_invalid"
