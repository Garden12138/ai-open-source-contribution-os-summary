"""Frozen Plan14 bytes under fresh synthetic fixture authority, no live database."""
import hashlib
import json
from pathlib import Path

from app.agent_v2.contracts import ToolResult
from app.agent_v2.tools import artifact_page
from app.provenance import content_hash
from tests.test_native_read_batches import ReadGateway

RECORDS = Path(__file__).parents[1] / "docs/agent-harness-reset-v1/read-batch-scheduler-v1"
CONTEXT_ID = "0d43ba945939aecb9b34b4ec0d6f97ca70a3a5a48ed967b521804c666e5c4834"
raw_context = (RECORDS / "plan14-execution/artifacts" / (CONTEXT_ID + ".json")).read_bytes()
assert hashlib.sha256(raw_context).hexdigest() == CONTEXT_ID
CONTEXT = json.loads(raw_context)
CONTENTS = {e["path"]: e["content"] for e in CONTEXT["entries"] if e.get("content") is not None}
assert all(hashlib.sha256(text.encode()).hexdigest() == next(e["prior_hash"] for e in CONTEXT["entries"] if e["path"] == path)
    for path, text in CONTENTS.items())
SOURCE = "7f02d8377a922ec5473bd68159817d058064e8a2892f65ef33ba17362bf9265c"


class FrozenGateway(ReadGateway):
    """Deterministic frozen-byte substitute; actual OCI tests use the real archive."""
    async def preflight_read_batch(self, spec, calls, *, source_hash, snapshot, input_hash):
        from app.agent_v2.tools import RestrictedToolRegistry
        from app.agent_v2.read_capacity import size
        self.preflights += 1
        args = [RestrictedToolRegistry().validate(c, spec).model_dump() for c in calls]
        checked = []
        for a in args:
            raw = CONTENTS[a["path"]].encode(); page = artifact_page(raw, a["offset"], a["max_bytes"])
            data = {"tool": "read_file", "execution_backend": "docker", "path": a["path"],
                "content_hash": hashlib.sha256(raw).hexdigest(), **page}
            checked.append({"availability": "present", "path": a["path"], "offset": a["offset"], "max_bytes": a["max_bytes"],
                "total_bytes": len(raw), "content_hash": data["content_hash"], "page_bytes": len(page["text"].encode()),
                "data_json_bytes": size(data), "data_hash": content_hash(data)})
        return {"version": "native-read-batch-preflight-v3", "input_hash": input_hash,
            "binding_hash": content_hash(spec.binding.model_dump()), "source_hash": SOURCE, "after_hash": SOURCE,
            "requests_hash": content_hash(args), "status": "succeeded", "failure_code": None,
            "checked": checked, "execution_backend": "fixture"}

    async def execute(self, spec, call, args, *, source_hash, snapshot, input_hash):
        self.calls.append(call)
        raw = CONTENTS[args.path].encode()
        data = {"tool": "read_file", "execution_backend": "docker", "path": args.path,
            "content_hash": hashlib.sha256(raw).hexdigest(), **artifact_page(raw, args.offset, args.max_bytes)}
        return {"result": ToolResult(call_id=call.call_id, input_hash=input_hash, source_hash=source_hash,
            output_source_hash=SOURCE, status="succeeded", data=data).model_dump()}
