"""Reuse only successful, exact read receipts bound to current immutable source."""
from __future__ import annotations

from typing import Any
from app.agent_v2.contracts import ToolCall, ToolResult
from app.agent_v2.progress import current_progress, enabled

READ_ONLY = frozenset({"read_file", "list_paths", "search_code", "read_diff", "read_artifact", "inspect_failure"})


def reuse_read(journal: Any, checkpoint: Any, call: ToolCall, arguments: Any, input_hash: str) -> ToolResult | None:
    if not enabled(journal) or call.name not in READ_ONLY:
        return None
    progress = current_progress(journal)
    phase = progress.phase if progress else None
    args = arguments.model_dump()
    for event in reversed(journal.events()):
        if (event.kind != "agent_v2_tool_result" or event.payload.get("name") != call.name
                or event.payload.get("request_arguments") != args or event.payload.get("progress_phase") != phase):
            continue
        aid = event.payload["artifact_id"]
        result = ToolResult.model_validate(journal.wb.read(aid))
        if (result.status != "succeeded" or result.failure is not None
                or result.output_source_hash != checkpoint.source_hash or result.data.get("reused_receipt")):
            continue
        data = result.data
        if (result.call_id != event.payload["call_id"] or result.source_hash != event.payload["source_hash"]
                or data.get("tool") != call.name):
            continue
        if (not isinstance(data.get("text"), str) or data.get("offset") != args.get("offset", 0)
                or type(data.get("total_bytes")) is not int):
            continue
        size = len(data["text"].encode())
        end = data["offset"] + size
        if not (0 <= data["offset"] <= end <= data["total_bytes"] and size <= args.get("max_bytes", 4096)):
            continue
        if data.get("next_offset") != (end if end < data["total_bytes"] else None):
            continue
        if call.name == "read_file" and data.get("path") != args["path"]:
            continue
        if call.name == "read_file" and args.get("expected_hash") not in {None, result.data.get("content_hash")}:
            continue
        data = {k: v for k, v in result.data.items() if k != "source_read_progress"}
        data.update(reused_receipt=aid, execution_backend="platform_receipt_reuse")
        return ToolResult(call_id=call.call_id, input_hash=input_hash, source_hash=checkpoint.source_hash,
            output_source_hash=checkpoint.source_hash, status="succeeded", data=data)
    return None
