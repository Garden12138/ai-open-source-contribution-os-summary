"""Source-bound file existence facts; planned write paths are never evidence."""
from __future__ import annotations

from typing import Any

from app.agent_v2.contracts import MissingSourceObservation, ReadFile, TaskSpec, ToolResult
from app.provenance import content_hash
from app.workbench import WorkbenchError


def validate_missing_read(spec: TaskSpec, arguments: ReadFile, result: ToolResult) -> None:
    observation = MissingSourceObservation.model_validate(result.data)
    if (result.status != "not_found" or result.failure is not None
            or observation.path != arguments.path or arguments.expected_hash is not None
            or observation.path not in spec.write_paths
            or observation.binding_hash != content_hash(spec.binding.model_dump())
            or observation.inventory_hash != result.output_source_hash):
        raise ValueError("agent_missing_source_evidence_invalid")


def file_context(journal: Any, snapshot: dict | None, planned_inspection: list[str]) -> dict | None:
    spec, cp = journal.spec(), journal.checkpoint()
    binding = journal.wb.binding(journal.task_id, spec.binding.plan_id)
    context = next((e for e in journal.wb.history(journal.task_id)
        if e.kind == "context_ready" and e.id == binding.payload["context_id"]), None)
    if context is None or context.record_hash != binding.payload["context_hash"]:
        raise WorkbenchError("冻结文件库存已变化。", code="agent_source_context_stale")
    if context.payload["archive_hash"] != spec.binding.archive_hash:
        raise WorkbenchError("冻结文件库存来源不匹配。", code="agent_source_context_stale")
    inventory = journal.wb.read(context.payload["artifact_id"])["inventory"]
    if not isinstance(inventory, list) or any(not isinstance(p, str) for p in inventory):
        raise WorkbenchError("冻结文件库存无效。", code="agent_source_context_stale")
    present = set(inventory)
    if snapshot:
        if snapshot["archive_hash"] != spec.binding.archive_hash or snapshot["source_hash"] != cp.source_hash:
            raise WorkbenchError("当前文件快照来源不匹配。", code="agent_source_context_stale")
        present.update(snapshot["files"])
    paths = set(spec.write_paths) | set(planned_inspection)
    return {"version": "approved-file-context-v1", "source_hash": cp.source_hash,
        "archive_hash": spec.binding.archive_hash, "frozen_inventory_artifact": context.payload["artifact_id"],
        "frozen_context_hash": context.record_hash, "snapshot_artifact": cp.snapshot_artifact,
        "existing_relevant_paths": sorted(paths & present), "planned_write_paths": spec.write_paths,
        "approved_new_paths_currently_absent": sorted(set(spec.write_paths) - present),
        "instruction": "Write permission does not prove existence. These facts come from frozen inventory and current snapshots. Confirm absence in OCI with read_file; not_found has no contents/hash. Create only approved absent paths using apply_patch with expected_hash null and old_text empty."}
