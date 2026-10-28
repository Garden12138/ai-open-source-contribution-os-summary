"""Tool schemas are authorization-independent. Reads never widen write scope."""
from app.agent_v2.contracts import (ApplyPatch, InspectFailure, ListPaths, Page,
    ReadFile, RunChecks, SearchCode, TaskSpec, ToolCall)
from app.workbench import WorkbenchError
from pydantic import BaseModel


def artifact_page(raw: bytes, offset: int, max_bytes: int) -> dict:
    """Return exact UTF-8 pages; continuation offsets never discard source bytes."""
    if offset < 0 or max_bytes < 4 or offset > len(raw) or (offset < len(raw) and raw[offset] & 0xC0 == 0x80):
        raise WorkbenchError("证据分页偏移无效。", code="agent_artifact_page_invalid")
    end = min(len(raw), offset + max_bytes)
    while end < len(raw) and raw[end] & 0xC0 == 0x80:
        end -= 1
    return {"offset": offset, "text": raw[offset:end].decode("utf-8"),
        "next_offset": end if end < len(raw) else None, "total_bytes": len(raw)}


class RestrictedToolRegistry:
    schemas = {"list_paths": ListPaths, "search_code": SearchCode, "read_file": ReadFile,
        "apply_patch": ApplyPatch, "read_diff": Page, "run_approved_checks": RunChecks,
        "inspect_failure": InspectFailure, "read_artifact": InspectFailure, "get_task_status": RunChecks}

    def validate(self, call: ToolCall, spec: TaskSpec) -> BaseModel:
        value = self.schemas[call.name].model_validate(call.arguments)
        if call.name == "apply_patch" and value.path not in spec.write_paths:
            raise WorkbenchError("修改路径不在已批准范围内。", code="agent_write_scope_denied")
        return value

    def descriptions(self) -> dict:
        return {name: schema.model_json_schema() for name, schema in self.schemas.items()}

    @staticmethod
    def cost(call: ToolCall) -> dict[str, int]:
        return {"writes" if call.name == "apply_patch" else
                "checks" if call.name == "run_approved_checks" else "reads": 1}
