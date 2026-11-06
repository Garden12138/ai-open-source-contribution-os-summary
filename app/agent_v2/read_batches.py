"""Bounded native read batches on the existing four-call checkpoint contract."""
from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from app.agent_v2.contracts import Hash, StrictModel, ToolCall
from app.agent_v2.tools import RestrictedToolRegistry
from app.provenance import content_hash
from app.workbench import WorkbenchError

POLICY = {"version": "native-read-batch-v1", "eligible_tool": "read_file",
    "original_calls_max": 8, "sub_batch_calls_max": 4, "admission": "whole_batch_before_execution",
    "sandbox_preflight": "all_paths_hashes_ranges_before_first_read", "failure": "stop_remaining_keep_receipts",
    "budget": "reserve_all_reads_no_refund", "observation": "after_original_batch_only"}
POLICY.update(preflight_contract="native-read-batch-preflight-v2",
    missing_source="OCI_attested_approved_new_path_not_found_v1")


class NativeReadBatch(StrictModel):
    """A provider proposal, never a widened executable Decision."""
    version: Literal["native-read-batch-v1"] = "native-read-batch-v1"
    calls: list[ToolCall] = Field(min_length=5, max_length=8)

    @model_validator(mode="after")
    def shape(self) -> NativeReadBatch:
        if any(c.name != "read_file" for c in self.calls) or len({c.call_id for c in self.calls}) != len(self.calls):
            raise ValueError("native_read_batch_invalid")
        return self


class ReadPathCheck(StrictModel):
    path: str = Field(max_length=500)
    content_hash: Hash
    total_bytes: int = Field(ge=0, le=1000000)
    offset: int = Field(ge=0, le=8000000)
    max_bytes: int = Field(ge=128, le=16000)


class ReadBatchPreflight(StrictModel):
    version: Literal["native-read-batch-preflight-v1"]
    input_hash: Hash
    binding_hash: Hash
    source_hash: Hash
    after_hash: Hash
    requests_hash: Hash
    status: Literal["succeeded", "rejected"]
    failure_code: str | None = Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")
    checked: list[ReadPathCheck] = Field(max_length=8)
    execution_backend: Literal["docker", "fixture"]

    @model_validator(mode="after")
    def coherent(self) -> ReadBatchPreflight:
        if (self.source_hash != self.after_hash
                or self.status == "succeeded" and (self.failure_code is not None or not self.checked)
                or self.status == "rejected" and (self.failure_code is None or self.checked)):
            raise ValueError("read_preflight_incoherent")
        return self


class PresentReadPathCheck(ReadPathCheck):
    availability: Literal["present"]


class MissingReadPathCheck(StrictModel):
    path: str = Field(max_length=500)
    offset: int = Field(ge=0, le=8000000)
    max_bytes: int = Field(ge=128, le=16000)
    availability: Literal["not_found"]
    reason_code: Literal["source_not_found"]
    creation_allowed: Literal[True]


class ReadBatchPreflightV2(ReadBatchPreflight):
    version: Literal["native-read-batch-preflight-v2"]
    checked: list[Annotated[PresentReadPathCheck | MissingReadPathCheck,
        Field(discriminator="availability")]] = Field(max_length=8)


def parse_preflight(value: dict) -> dict:
    cls = ReadBatchPreflightV2 if value.get("version") == "native-read-batch-preflight-v2" else ReadBatchPreflight
    return cls.model_validate(value).model_dump()


def checked_path_valid(check: dict, args: dict, write_paths: list[str]) -> bool:
    if any(check[k] != args[k] for k in ("path", "offset", "max_bytes")):
        return False
    if check.get("availability") == "not_found":
        return args["path"] in write_paths and args["expected_hash"] is None
    return (0 <= args["offset"] <= check["total_bytes"] <= 1000000
        and args["expected_hash"] in {None, check["content_hash"]})


def platform_calls(model_job: str, proposal: NativeReadBatch) -> list[ToolCall]:
    return [c.model_copy(update={"call_id": "tool-" + content_hash([model_job, c.call_id])[:40]}) for c in proposal.calls]


def active(journal: Any) -> Any | None:
    events = journal.events()
    root = next((e for e in reversed(events) if e.kind == "agent_v2_read_batch"), None)
    if root and not any(e.kind in {"agent_v2_read_batch_completed", "agent_v2_read_batch_stopped"}
            and e.payload.get("batch_artifact") == root.payload["artifact_id"] for e in events):
        return root
    return None


def record(journal: Any, root: Any) -> dict:
    value = journal.wb.read(root.payload["artifact_id"])
    proposal = NativeReadBatch.model_validate(value["proposal"])
    spec = journal.spec()
    expected_children = [{"index": i // 4,
        "id": content_hash([value["response_artifact"], spec.binding.model_dump(), i // 4]),
        "call_ids": [c.call_id for c in platform_calls(root.job_id, proposal)[i:i + 4]]}
        for i in range(0, len(proposal.calls), 4)]
    if (value["version"] != POLICY["version"] or value["binding_hash"] != content_hash(spec.binding.model_dump())
            or value["model_job_id"] != root.job_id or root.actor_id != "orchestrator"
            or value["reads_reserved"] != len(proposal.calls) or value["sub_batches"] != expected_children
            or value["calls"] != [c.model_dump() for c in platform_calls(root.job_id, proposal)]):
        raise WorkbenchError("读取批次来源无效。", code="agent_read_batch_invalid")
    return value


def preflight(journal: Any, root: Any) -> Any | None:
    return next((e for e in journal.events() if e.kind == "agent_v2_read_batch_preflight"
        and e.payload.get("batch_artifact") == root.payload["artifact_id"]), None)


def admit(journal: Any, checkpoint: Any, proposal: NativeReadBatch, *, response_artifact: str,
          model_job_id: str, usage: dict) -> None:
    """Validate the complete proposal and reserve all reads before any child exists."""
    proposal = NativeReadBatch.model_validate(proposal.model_dump())
    spec = journal.spec()
    from app.sandbox_worker.specs import SandboxPolicy
    if (checkpoint.state != "model" or spec.model_transport is None
            or spec.binding.policy_hash != SandboxPolicy().policy_hash or active(journal)):
        raise WorkbenchError("读取批次沙箱或状态不匹配。", code="agent_read_batch_invalid")
    raw = journal.wb.read(response_artifact)
    source = next((e for e in journal.events() if e.kind == "agent_v2_model_response"
        and e.job_id == model_job_id and e.payload["artifact_id"] == response_artifact), None)
    if source is None or raw["request_id"] != model_job_id or raw["binding_hash"] != content_hash(spec.binding.model_dump()):
        raise WorkbenchError("读取批次没有原始响应来源。", code="agent_read_batch_invalid")
    vendors = raw["choices"][0]["tool_calls"]
    if len(vendors) != len(proposal.calls):
        raise WorkbenchError("读取批次数量不匹配。", code="agent_read_batch_invalid")
    registry = RestrictedToolRegistry()
    bound = 0
    for index, call in enumerate(proposal.calls):
        vendor = vendors[index]
        if (vendor["function"]["name"] != call.name or json.loads(vendor["function"]["arguments"]) != call.arguments
                or call.call_id != "native-" + content_hash([model_job_id, index, vendor["id"]])[:32]):
            raise WorkbenchError("读取批次调用来源不匹配。", code="agent_read_batch_invalid")
        args = registry.validate(call, spec)
        # JSON escaping plus bounded receipt/progress/failure metadata. This is
        # admission capacity, not a refund or an expansion of output allowance.
        bound += 6 * args.max_bytes + 8192
    if bound > journal.remaining(checkpoint)["output_bytes"]:
        raise WorkbenchError("读取批次输出预算不足。", code="agent_read_batch_budget")
    reserved = journal.charge(checkpoint.model_copy(update={"usage": usage}), {"reads": len(proposal.calls)})
    calls = platform_calls(model_job_id, proposal)
    children = [{"index": i // 4, "id": content_hash([response_artifact, spec.binding.model_dump(), i // 4]),
        "call_ids": [c.call_id for c in calls[i:i + 4]]} for i in range(0, len(calls), 4)]
    value = {"version": POLICY["version"], "proposal": proposal.model_dump(), "model_job_id": model_job_id,
        "response_artifact": response_artifact, "binding_hash": content_hash(spec.binding.model_dump()),
        "admission_source_hash": checkpoint.source_hash, "policy_hash": spec.binding.policy_hash,
        "calls": [c.model_dump() for c in calls], "sub_batches": children,
        "reads_reserved": len(calls), "output_bytes_admission_bound": bound}
    aid = journal.wb.artifact(value)
    journal.append("read_batch", {"artifact_id": aid, "response_artifact": response_artifact,
        "call_count": len(calls), "reads_reserved": len(calls), "source_hash": checkpoint.source_hash,
        "binding_hash": value["binding_hash"]}, key="read-batch:" + model_job_id, job_id=model_job_id)
    journal.transition(checkpoint, state="tools", usage=reserved, pending_calls=calls[:4],
        key="read-batch-cp:" + model_job_id, job_id=model_job_id)


def child_started(journal: Any, root: Any, checkpoint: Any, index: int, *, job_id: str) -> None:
    value = record(journal, root)
    child = value["sub_batches"][index]
    journal.append("read_sub_batch_started", {"batch_artifact": root.payload["artifact_id"],
        "sub_batch_id": child["id"], "index": index, "checkpoint_hash": checkpoint.revision,
        "source_hash": checkpoint.source_hash, "call_ids": child["call_ids"]},
        key="read-child-start:" + child["id"], job_id=job_id)


def source_hash(journal: Any, root: Any) -> str:
    proof = preflight(journal, root)
    if proof is None or proof.payload["status"] != "succeeded":
        raise WorkbenchError("读取批次尚未完成整批预检。", code="agent_read_batch_preflight_missing")
    return proof.payload["source_hash"]


def child_index(journal: Any, root: Any, call_id: str) -> int:
    value = record(journal, root)
    return next(i for i, child in enumerate(value["sub_batches"]) if call_id in child["call_ids"])


def stop(journal: Any, root: Any, *, code: str, job_id: str, evidence: str | None = None) -> None:
    value = record(journal, root)
    receipts = [e for e in journal.events() if e.kind == "agent_v2_tool_result"
        and e.payload.get("read_batch_artifact") == root.payload["artifact_id"]]
    completed = {e.payload["call_id"] for e in receipts}
    started = {e.payload["call_id"] for e in journal.events() if e.kind == "agent_v2_tool_started"
        and e.payload["call_id"] in {c["call_id"] for c in value["calls"]}}
    data = {"batch_artifact": root.payload["artifact_id"], "reason_code": code, "evidence": evidence,
        "actual_receipts": [e.payload["artifact_id"] for e in receipts],
        "unknown_calls": [c["call_id"] for c in value["calls"] if c["call_id"] in started - completed],
        "not_executed": [c["call_id"] for c in value["calls"] if c["call_id"] not in completed | started]}
    journal.append("read_batch_stopped", {**data, "artifact_id": journal.wb.artifact(data)},
        key="read-batch-stop:" + root.payload["artifact_id"], job_id=job_id)


def complete_child(journal: Any, root: Any, checkpoint: Any, index: int, *, job_id: str,
                   stopped: bool) -> list[ToolCall]:
    value = record(journal, root)
    child = value["sub_batches"][index]
    results = [e for e in journal.events() if e.kind == "agent_v2_tool_result"
        and e.payload.get("read_batch_artifact") == root.payload["artifact_id"]]
    by_call = {e.payload["call_id"]: e for e in results}
    ids = [cid for cid in child["call_ids"] if cid in by_call]
    if not stopped and ids != child["call_ids"]:
        raise WorkbenchError("读取子批次缺少真实回执。", code="agent_read_batch_receipt_missing")
    receipt = {"version": "native-read-sub-batch-result-v1", "batch_artifact": root.payload["artifact_id"],
        "sub_batch_id": child["id"], "index": index, "status": "stopped" if stopped else "succeeded",
        "source_hash": checkpoint.source_hash, "binding_hash": value["binding_hash"],
        "call_ids": ids, "receipts": [by_call[cid].payload["artifact_id"] for cid in ids]}
    journal.append("read_sub_batch_completed", {**{k: v for k, v in receipt.items() if k != "version"},
        "artifact_id": journal.wb.artifact(receipt)},
        key="read-child-complete:" + child["id"], job_id=job_id)
    if stopped:
        return []
    if index + 1 < len(value["sub_batches"]):
        ids = value["sub_batches"][index + 1]["call_ids"]
        return [ToolCall.model_validate(c) for c in value["calls"] if c["call_id"] in ids]
    ids = [c["call_id"] for c in value["calls"]]
    if list(by_call) != ids:
        raise WorkbenchError("原始读取批次回执顺序无效。", code="agent_read_batch_receipt_invalid")
    data = {"version": "native-read-batch-result-v1", "batch_artifact": root.payload["artifact_id"],
        "response_artifact": value["response_artifact"], "source_hash": checkpoint.source_hash,
        "binding_hash": value["binding_hash"], "call_ids": ids,
        "receipts": [by_call[cid].payload["artifact_id"] for cid in ids]}
    journal.append("read_batch_completed", {**{k: v for k, v in data.items() if k != "version"},
        "artifact_id": journal.wb.artifact(data)},
        key="read-batch-complete:" + root.payload["artifact_id"], job_id=job_id)
    return []
