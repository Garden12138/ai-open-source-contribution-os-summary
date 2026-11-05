"""Bounded native response evidence, recorded before any tool Decision exists."""
from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

from app.provenance import canonical_json, content_hash
from app.security import SensitiveDataError, ensure_no_sensitive_data, redact_text
from app.workbench import WorkbenchError

VERSION = "agent-model-response-v1"


def response_evidence(value: Mapping[str, Any], *, request_id: str, binding_hash: str) -> dict:
    """Keep exact call envelopes/order, excluding assistant text and private reasoning.

    A credential-bearing field is redacted and the entire response becomes
    non-executable. Malformed JSON arguments remain strings for diagnosis.
    """
    choices = value.get("choices")
    projected = []
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, Mapping):
                projected.append({"invalid_choice": True})
                continue
            message = choice.get("message")
            message = message if isinstance(message, Mapping) else {}
            calls = message.get("tool_calls")
            envelopes = []
            if isinstance(calls, list):
                for call in calls:
                    if not isinstance(call, Mapping):
                        envelopes.append({"invalid_call": True})
                        continue
                    function = call.get("function")
                    item = {k: call[k] for k in ("id", "type") if k in call}
                    item["function"] = ({k: function[k] for k in ("name", "arguments") if k in function}
                        if isinstance(function, Mapping) else None)
                    envelopes.append(item)
            projected.append({"finish_reason": choice.get("finish_reason"), "role": message.get("role"),
                "tool_calls": envelopes if isinstance(calls, list) else None})
    result = {"version": VERSION, "request_id": request_id, "binding_hash": binding_hash,
        "choices": projected, "usage": value.get("usage"), "sensitive_redacted": False}
    try:
        ensure_no_sensitive_data(result, context="native response envelope")
    except SensitiveDataError:
        def safe(item: Any) -> Any:
            if isinstance(item, str):
                return redact_text(item)
            if isinstance(item, Mapping):
                return {redact_text(k): safe(v) for k, v in item.items()}
            if isinstance(item, list):
                return [safe(v) for v in item]
            return item
        result = safe(result)
        result["sensitive_redacted"] = True
    ensure_no_sensitive_data(result, context="native response envelope")
    return result


def original_call(journal: Any, call_id: str) -> dict:
    """Derive the unique vendor call from a committed, accepted native turn."""
    for turn in reversed(journal.events()):
        if turn.kind != "agent_v2_turn" or not turn.payload.get("response_artifact"):
            continue
        for index, call in enumerate(turn.payload["decision"]["calls"]):
            if "tool-" + content_hash([turn.job_id, call["call_id"]])[:40] != call_id:
                continue
            aid = turn.payload["response_artifact"]
            raw = journal.wb.read(aid)
            try:
                vendor = raw["choices"][0]["tool_calls"][index]
                expected = "native-" + content_hash([turn.job_id, index, vendor["id"]])[:32]
                if (raw["version"] != VERSION or raw["request_id"] != turn.job_id or call["call_id"] != expected
                        or raw["binding_hash"] != content_hash(journal.spec().binding.model_dump())
                        or vendor["function"]["name"] != call["name"]
                        or json.loads(vendor["function"]["arguments"]) != call["arguments"]):
                    raise ValueError()
            except (KeyError, IndexError, TypeError, ValueError):
                raise WorkbenchError("模型调用来源无效。", code="agent_model_response_binding_invalid") from None
            return {"response_artifact": aid, "provider_call_id": vendor["id"], "provider_call_index": index}
    return {}  # Legacy turns never acquire invented vendor provenance.
