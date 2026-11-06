"""Source-attested read capacity and exact output artifact metering.

Output bytes cover model envelopes, final ToolResults, observed unsafe results
when affordable, and batch/proof/certificate/result artifacts. Journal/control
checkpoints and progress-ledger references are separate bounded audit storage;
they contain no additional source page bodies. Reservations are not usage.
"""
from __future__ import annotations

from typing import Any
from hashlib import sha256

from app.agent_v2.contracts import ToolResult
from app.agent_v2.read_progress import ReadProgressNote
from app.provenance import canonical_json, content_hash
from app.workbench import WorkbenchError

VERSION = "native-read-capacity-v1"
FAILURE_BYTES = 8192  # Enforced bound for the platform's diagnostic envelope.
H = "f" * 64


def size(value: Any) -> int:
    return len(canonical_json(value).encode("utf-8"))


def store(journal: Any, value: dict, usage: dict, *, role: str) -> str:
    """Charge the exact immutable bytes, in the same transaction as the checkpoint."""
    amount = size(value)
    # Dispatch enforces wall time. Committing already-observed truth or a stop
    # record must remain possible after that deadline, within the byte reserve.
    charged = dict(usage)
    total = charged.get("output_bytes", 0) + amount
    if total > journal.spec().limits.output_bytes:
        raise WorkbenchError("输出证据预算不足。", code="agent_budget_exceeded")
    charged["output_bytes"] = total
    aid = journal.wb.artifact(value)
    journal.append("output_charge", {"artifact_id": aid, "bytes": amount, "role": role,
        "contract": VERSION}, key="output-charge:" + role + ":" + aid)
    usage.clear(); usage.update(charged)
    return aid


def source_page(journal: Any, text: str, usage: dict) -> str:
    """Issue only already-observed bytes, billed once per session and hash."""
    raw = text.encode("utf-8")
    aid = sha256(raw).hexdigest()
    prior = next((e for e in journal.events() if e.kind == "agent_v2_output_charge"
        and e.payload["role"] == "source_page" and e.payload["artifact_id"] == aid), None)
    if prior is not None:
        if prior.payload["bytes"] != len(raw) or journal.wb.artifacts.read_bytes(aid) != raw:
            raise WorkbenchError("源码页证据失效。", code="agent_source_page_invalid")
        return aid
    total = usage.get("output_bytes", 0) + len(raw)
    if total > journal.spec().limits.output_bytes:
        raise WorkbenchError("输出证据预算不足。", code="agent_budget_exceeded")
    stored = journal.wb.artifacts.store_bytes(raw, media_type="text/plain; charset=utf-8", commit=False)
    journal.append("output_charge", {"artifact_id": stored.id, "bytes": len(raw), "role": "source_page",
        "contract": VERSION}, key="output-charge:source_page:" + stored.id)
    usage["output_bytes"] = total
    return stored.id


def progress_bound() -> int:
    note = ReadProgressNote(range_key=H, max_bytes=16000, prior_same_range_reads=256,
        prior_receipt=H, warning_receipts_seen=[H, H], visible_bytes=16000, visible_next_offset=1000000)
    # Merge size includes the comma, and accommodates reuse's longer backend.
    return size({"source_read_progress": note.model_dump(), "reused_receipt": H}) + len("platform_receipt_reuse")


def receipt_bound(call_id: str, data_bytes: int) -> int:
    outer = ToolResult(call_id=call_id, input_hash=H, source_hash=H, output_source_hash=H,
        artifact_id=H, status="succeeded", data={}).model_dump()
    return size(outer) - 2 + data_bytes + progress_bound() + size({"source_page_artifact": H})


def validate_claim(check: dict, args: dict, spec: Any, source_hash: str) -> None:
    if check.get("availability") == "not_found":
        from app.agent_v2.contracts import MissingSourceObservation
        data = MissingSourceObservation(tool="read_file", path=args["path"], availability="not_found",
            reason_code="source_not_found", creation_allowed=True, binding_hash=content_hash(spec.binding.model_dump()),
            inventory_hash=source_hash, execution_backend="docker").model_dump(exclude_none=True)
        if check["page_bytes"] != 0 or check["data_json_bytes"] != size(data) or check["data_hash"] != content_hash(data):
            raise ValueError("read_capacity_absence_invalid")
        return
    count = check["page_bytes"]
    if (not 0 <= count <= min(args["max_bytes"], check["total_bytes"] - args["offset"])
            or count == 0 and args["offset"] != check["total_bytes"]):
        raise ValueError("read_capacity_page_invalid")
    end = args["offset"] + count
    empty = {"tool": "read_file", "execution_backend": "docker", "path": args["path"], "text": "",
        "offset": args["offset"], "next_offset": end if end < check["total_bytes"] else None,
        "total_bytes": check["total_bytes"], "content_hash": check["content_hash"]}
    if not size(empty) + count <= check["data_json_bytes"] <= size(empty) + 6 * count:
        raise ValueError("read_capacity_serialization_invalid")


def structural_bound(batch: dict, source_hash: str) -> int:
    """Upper bound both completion and a stopped prefix; IDs retain actual lengths."""
    ids = [c["call_id"] for c in batch["calls"]]
    common = {"batch_artifact": H, "source_hash": source_hash, "binding_hash": H}
    children = [{"version": "native-read-sub-batch-result-v1", **common,
        "sub_batch_id": c["id"], "index": c["index"], "status": "succeeded",
        "call_ids": c["call_ids"], "receipts": [H] * len(c["call_ids"])} for c in batch["sub_batches"]]
    aggregate = {"version": "native-read-batch-result-v1", **common, "response_artifact": H,
        "call_ids": ids, "receipts": [H] * len(ids)}
    stopped = {"batch_artifact": H, "reason_code": "x" * 100, "evidence": H,
        "actual_receipts": [H] * len(ids), "unknown_calls": ids, "not_executed": ids}
    return sum(map(size, children)) + size(aggregate) + size(stopped) + FAILURE_BYTES


def preflight_bound(requests: list[dict], batch: dict) -> int:
    checked = [{"path": a["path"], "offset": a["offset"], "max_bytes": a["max_bytes"],
        "availability": "not_found", "content_hash": H, "total_bytes": 1000000,
        "reason_code": "source_not_found", "creation_allowed": True,
        "data_json_bytes": 100000, "data_hash": H, "page_bytes": 16000} for a in requests]
    proof = {"version": "native-read-batch-preflight-v3", "input_hash": H, "binding_hash": H,
        "source_hash": H, "after_hash": H, "requests_hash": H, "status": "succeeded",
        "failure_code": "x" * 100, "checked": checked, "execution_backend": "fixture"}
    return size(proof) + structural_bound(batch, H)


def certificate(batch: dict, proof: dict, *, batch_artifact: str, proof_artifact: str,
                remaining: int) -> dict:
    if proof["version"] != "native-read-batch-preflight-v3":
        raise ValueError("read_capacity_proof_version_invalid")
    calls = []
    for call, check in zip(batch["calls"], proof["checked"], strict=True):
        normal = receipt_bound(call["call_id"], check["data_json_bytes"])
        calls.append({"call_id": call["call_id"], "data_hash": check["data_hash"],
            "data_json_bytes": check["data_json_bytes"], "result_bytes_bound": normal + FAILURE_BYTES,
            # Either a valid receipt + raw source page (<=16000), or an
            # observed invalid result + bounded diagnostic (2*8192). These
            # branches are exclusive; reservations are never charged as usage.
            "output_bytes_bound": normal + 2 * FAILURE_BYTES})
    value = {"version": VERSION, "batch_artifact": batch_artifact, "preflight_artifact": proof_artifact,
        "source_hash": proof["source_hash"], "binding_hash": batch["binding_hash"],
        "remaining_output_bytes": remaining, "calls": calls, "status": "succeeded",
        "metadata_bytes_bound": structural_bound(batch, proof["source_hash"]), "total_bytes_bound": 0}
    # Certificate is itself charged; use maximal numeric/status widths for its bound.
    overhead = size({**value, "total_bytes_bound": 4000000, "status": "rejected"})
    value["total_bytes_bound"] = sum(c["output_bytes_bound"] for c in calls) + value["metadata_bytes_bound"] + overhead
    if value["total_bytes_bound"] > remaining:
        value["status"] = "rejected"
    return value


def attested_entry(journal: Any, batch: Any, call_id: str) -> dict | None:
    original = journal.wb.read(batch.payload["artifact_id"])
    if original.get("capacity_contract") != VERSION:
        return None  # Legacy batches keep their original already-admitted bound.
    event = next((e for e in journal.events() if e.kind == "agent_v2_read_capacity"
        and e.payload["batch_artifact"] == batch.payload["artifact_id"]), None)
    if event is None:
        raise WorkbenchError("读取容量证明缺失。", code="agent_read_capacity_missing")
    value = journal.wb.read(event.payload["artifact_id"])
    proof_event = next(e for e in journal.events() if e.kind == "agent_v2_read_batch_preflight"
        and e.payload["batch_artifact"] == batch.payload["artifact_id"])
    expected = certificate(original, journal.wb.read(proof_event.payload["artifact_id"]),
        batch_artifact=batch.payload["artifact_id"], proof_artifact=proof_event.payload["artifact_id"],
        remaining=value["remaining_output_bytes"])
    if (value != expected or event.actor_id != "orchestrator" or value["status"] != "succeeded"
            or value["source_hash"] != journal.checkpoint().source_hash):
        raise WorkbenchError("读取容量证明已失效。", code="agent_read_capacity_stale")
    return next(c for c in value["calls"] if c["call_id"] == call_id)


def observation_matches(result: ToolResult, entry: dict) -> bool:
    data = {k: v for k, v in result.data.items() if k not in {"source_read_progress", "reused_receipt"}}
    if "source_page_artifact" in data:
        if not isinstance(data.get("text"), str) or data.pop("source_page_artifact") != sha256(data["text"].encode()).hexdigest():
            return False
    # The trusted OCI commitment is to the original observation before cache metadata.
    if data.get("execution_backend") not in {"docker", "fixture", "platform_receipt_reuse"}:
        return False
    data["execution_backend"] = "docker"
    return size(data) == entry["data_json_bytes"] and content_hash(data) == entry["data_hash"]
