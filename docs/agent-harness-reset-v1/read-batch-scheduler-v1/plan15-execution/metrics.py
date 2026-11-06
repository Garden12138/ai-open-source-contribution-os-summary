"""Compute actual observation metrics from exported immutable real evidence only."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from app.agent_v2.native_tools import native_input_bound
from app.provenance import canonical_json, content_hash
from app.security import ensure_no_sensitive_data

ROOT = Path(__file__).resolve().parent


def length(ranges: list[tuple[int, int]]) -> int:
    end = 0; result = 0
    for start, stop in sorted(ranges):
        result += max(0, stop - max(start, end)); end = max(end, stop)
    return result


def main() -> None:
    index = json.loads((ROOT / "evidence-index.json").read_text())
    assert index["report_hash"] == content_hash({k: v for k, v in index.items() if k != "report_hash"})
    audit = json.loads((ROOT / "final-audit.json").read_text())
    artifacts = {row["id"]: ROOT / row["file"] for row in index["exported_artifacts"]}

    def raw(aid: str) -> bytes:
        value = artifacts[aid].read_bytes(); assert hashlib.sha256(value).hexdigest() == aid
        return value

    def read(aid: str) -> dict:
        return json.loads(raw(aid))

    events = index["events"]
    starts = [e for e in events if e["kind"] == "agent_v2_model_started"]
    responses = {e["job_id"]: e for e in events if e["kind"] == "agent_v2_model_response"}
    round_for_response = {e["payload"]["artifact_id"]: i for i, start in enumerate(starts, 1)
        if (e := responses.get(start["job_id"])) is not None}
    results = [e for e in events if e["kind"] == "agent_v2_tool_result"]
    receipts = {e["payload"]["artifact_id"]: read(e["payload"]["artifact_id"]) for e in results}
    returned = defaultdict(list); delivered = defaultdict(list)
    unique_calls = 0; exact_duplicates = 0; overlapping_calls = 0; returned_bytes = 0
    exact_seen = set(); offset_counts = Counter(); source_reads = []; missing = []; page_sources = {}
    for event in results:
        p = event["payload"]; value = receipts[p["artifact_id"]]; data = value["data"]
        if p["name"] != "read_file": continue
        if value["status"] == "not_found":
            missing.append({"path": data["path"], "receipt_artifact": p["artifact_id"], "event_sequence": event["sequence"]})
            continue
        if value["status"] != "succeeded" or not isinstance(data.get("text"), str): continue
        text = data["text"].encode(); offset = data["offset"]
        key = (value["output_source_hash"], data["path"], data["content_hash"])
        before = length(returned[key]); returned[key].append((offset, offset + len(text)))
        added = length(returned[key]) - before
        if added: unique_calls += 1
        exact = (*key, offset, offset + len(text), hashlib.sha256(text).hexdigest())
        duplicate = exact in exact_seen; exact_seen.add(exact)
        exact_duplicates += duplicate; overlapping_calls += added < len(text)
        offset_counts[(*key, offset)] += 1; returned_bytes += len(text)
        row = {"sequence": event["sequence"], "path": data["path"], "receipt_artifact": p["artifact_id"],
            "provider_call_id": p.get("provider_call_id"), "source_hash": value["source_hash"],
            "output_source_hash": value["output_source_hash"], "content_hash": data["content_hash"],
            "offset": offset, "bytes": len(text), "next_offset": data["next_offset"], "total_file_bytes": data["total_bytes"],
            "new_unique_bytes": added, "exact_duplicate": duplicate,
            "actual_or_reused": "reused" if data.get("reused_receipt") or data.get("execution_backend") == "platform_receipt_reuse" else "executed",
            "source_page_artifact": data.get("source_page_artifact")}
        source_reads.append(row)
        if data.get("source_page_artifact"):
            assert raw(data["source_page_artifact"]) == text
            page_sources[data["source_page_artifact"]] = row
    prompt_rows = []; recovery = defaultdict(list); missing_delivery = defaultdict(list)
    for ordinal, start in enumerate(starts, 1):
        prompt = read(start["payload"]["prompt_artifact"])
        responded = start["job_id"] in responses
        rows = []; artifact_rows = []
        for feedback in prompt.get("feedback", []):
            aid = feedback.get("receipt_artifact"); original = receipts.get(aid)
            if original is None: continue
            full, shown = original["data"], feedback.get("data", {})
            if feedback["status"] == "not_found":
                if responded: missing_delivery[aid].append({"round": ordinal, "prompt_artifact": start["payload"]["prompt_artifact"]})
                continue
            if shown.get("tool") == "read_file" and isinstance(shown.get("text"), str):
                chunk = shown["text"].encode(); offset = shown["offset"]
                assert chunk == full["text"].encode()[:len(chunk)] and offset == full["offset"]
                key = (original["output_source_hash"], full["path"], full["content_hash"])
                if responded: delivered[key].append((offset, offset + len(chunk)))
                rows.append({"path": full["path"], "content_hash": full["content_hash"], "receipt_artifact": aid,
                    "offset": offset, "tool_returned_bytes": len(full["text"].encode()), "inline_bytes": len(chunk),
                    "visible_next_offset": shown["next_offset"], "total_file_bytes": full["total_bytes"],
                    "source_page_artifact": full.get("source_page_artifact"), "partial": bool(feedback.get("inline_projection"))})
            if shown.get("tool") == "read_artifact" and isinstance(shown.get("text"), str):
                target = full["artifact_id"]; chunk = shown["text"].encode(); offset = shown["offset"]
                assert chunk == raw(target)[offset:offset + len(chunk)]
                recovered = {"artifact_id": target, "receipt_artifact": aid, "round": ordinal,
                    "prompt_artifact": start["payload"]["prompt_artifact"], "offset": offset,
                    "tool_returned_bytes": len(full["text"].encode()), "inline_bytes": len(chunk),
                    "visible_next_offset": shown["next_offset"], "total_bytes": shown["total_bytes"],
                    "actual_response_confirms_request": responded}
                artifact_rows.append(recovered)
                if responded:
                    recovery[target].append((offset, offset + len(chunk)))
                    if target in page_sources:
                        source = page_sources[target]
                        key = (source["output_source_hash"], source["path"], source["content_hash"])
                        delivered[key].append((source["offset"] + offset, source["offset"] + offset + len(chunk)))
        bound = native_input_bound(prompt); assert bound <= 32000
        prompt_rows.append({"round": ordinal, "job_id": start["job_id"], "event_sequence": start["sequence"],
            "prompt_artifact": start["payload"]["prompt_artifact"], "request_delivery": "response_confirmed" if responded else "unknown_no_response",
            "input_bound": bound, "source_rows": rows, "inline_source_bytes": sum(r["inline_bytes"] for r in rows),
            "raw_artifact_pages": artifact_rows, "approved_task_input": prompt.get("approved_task_input"),
            "task_context_artifact": prompt.get("task_context_artifact")})
    # read_artifact may carry JSON-escaped source inside a full ToolResult.
    # Map only completely delivered encoded characters back to source bytes;
    # availability of a whole registered artifact does not prove its delivery.
    encoded_receipt_source_coverage = []
    for target, ranges in recovery.items():
        if artifacts[target].suffix != ".json": continue
        original = read(target); data = original.get("data", {})
        if data.get("tool") != "read_file" or not isinstance(data.get("text"), str): continue
        encoded = canonical_json(data["text"]).encode()
        marker = b'"text":' + encoded; body = raw(target)
        assert body.count(marker) == 1
        cursor = body.index(marker) + len(b'"text":') + 1
        source_offset = data["offset"]; before = sum(length(v) for v in delivered.values())
        key = (original["output_source_hash"], data["path"], data["content_hash"])
        mapped = []
        for char in data["text"]:
            encoded_char = canonical_json(char).encode()[1:-1]
            assert body[cursor:cursor + len(encoded_char)] == encoded_char
            if any(start <= cursor and cursor + len(encoded_char) <= stop for start, stop in ranges):
                mapped.append((source_offset, source_offset + len(char.encode())))
            cursor += len(encoded_char); source_offset += len(char.encode())
        delivered[key].extend(mapped)
        encoded_receipt_source_coverage.append({"receipt_artifact": target, "path": data["path"],
            "source_hash": key[0], "content_hash": key[2], "unique_source_bytes_decodable_from_delivered_JSON": length(mapped),
            "additional_unique_source_bytes_beyond_direct_delivery": sum(length(v) for v in delivered.values()) - before})
    requested = []
    for start in starts:
        event = responses.get(start["job_id"])
        if event is None: continue
        response = read(event["payload"]["artifact_id"])
        for choice in response["choices"]:
            for call in choice.get("tool_calls") or []:
                requested.append({"round": round_for_response[event["payload"]["artifact_id"]], "response_artifact": event["payload"]["artifact_id"],
                    "provider_call_id": call["id"], "name": call["function"]["name"], "arguments": call["function"]["arguments"]})
    proposed = [r for r in requested if r["name"] == "apply_patch"]
    requested_reads = []
    requested_ranges = Counter()
    for call in requested:
        if call["name"] != "read_file": continue
        arguments = json.loads(call["arguments"]) if isinstance(call["arguments"], str) else call["arguments"]
        matched = [e for e in results if e["payload"].get("response_artifact") == call["response_artifact"]
            and e["payload"].get("provider_call_id") == call["provider_call_id"]]
        source = receipts[matched[0]["payload"]["artifact_id"]]["output_source_hash"] if matched else "unavailable"
        key = (source, arguments["path"], arguments.get("offset", 0), arguments.get("max_bytes", 4096))
        requested_ranges[key] += 1
        requested_reads.append({**call, "arguments": arguments, "same_request_seen_before": requested_ranges[key] > 1,
            "result_status": matched[0]["payload"]["status"] if matched else "not_dispatched_or_unavailable"})
    applied = [e for e in results if e["payload"]["name"] == "apply_patch" and receipts[e["payload"]["artifact_id"]]["status"] == "succeeded"]
    for item in missing:
        item["later_real_model_inputs"] = missing_delivery[item["receipt_artifact"]]
        item["status"] = "observed" if item["later_real_model_inputs"] else "not_observed"
    recovery_report = [{"artifact_id": aid, "kind": "source_page" if aid in page_sources else "Plan_or_other_issued_evidence",
        "total_bytes": len(raw(aid)), "unique_delivered_bytes": length(ranges),
        "complete_raw_artifact_delivered": length(ranges) == len(raw(aid)),
        "reconstructed_hash_verified": hashlib.sha256(raw(aid)).hexdigest() == aid,
        "delivery_ranges": ranges} for aid, ranges in recovery.items()]
    usage = [read(e["payload"]["artifact_id"])["usage"] for e in responses.values()]
    report = {"version": "output-capacity-plan15-actual-coding-metrics-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
        "classification": "derived_from_this_real_attempt_committed_events_and_exact_artifacts_no_fixture_results",
        "proposal_hash": audit["proposal_hash"], "plan_id": audit["plan_id"], "session_id": audit["session_id"],
        "execution_authorization_hash": audit["execution_authorization_hash"], "attempt_ids": audit["execution_attempts"],
        "model_requests_started": len(starts), "model_responses_preserved": len(responses),
        "actual_provider_usage": {
            "input_tokens": sum(u.get("prompt_tokens", u.get("input_tokens", 0)) for u in usage),
            "output_tokens": sum(u.get("completion_tokens", u.get("output_tokens", 0)) for u in usage),
            "cached_input_tokens": sum(u.get("prompt_cache_hit_tokens", u.get("prompt_tokens_details", {}).get("cached_tokens", 0)) for u in usage)},
        "charged_usage": audit["checkpoint"]["usage"], "requested_tool_counts": dict(Counter(c["name"] for c in requested)),
        "requested_source_reads": requested_reads,
        "repeated_exact_source_read_requests": sum(max(0, n - 1) for n in requested_ranges.values()),
        "successful_source_read_receipts": len(source_reads), "effective_source_read_calls_with_new_bytes": unique_calls,
        "source_bytes_returned_including_duplicates": returned_bytes,
        "unique_source_bytes_observed_by_tools": sum(length(v) for v in returned.values()),
        "unique_source_bytes_delivered_in_confirmed_real_prompts": sum(length(v) for v in delivered.values()),
        "exact_duplicate_source_receipts": exact_duplicates, "overlapping_source_receipts": overlapping_calls,
        "repeated_same_offset_successful_reads": sum(max(0, n - 1) for n in offset_counts.values()),
        "source_receipt_execution_counts": dict(Counter(r["actual_or_reused"] for r in source_reads)),
        "source_reads": source_reads, "prompt_inline_source_coverage": prompt_rows,
        "source_coverage_inside_delivered_receipt_JSON": encoded_receipt_source_coverage,
        "read_artifact_requested_calls": sum(c["name"] == "read_artifact" for c in requested),
        "artifact_pagination_recovery": recovery_report,
        "first_patch_proposal_round": proposed[0]["round"] if proposed else None,
        "first_patch_application_round": round_for_response.get(applied[0]["payload"]["response_artifact"]) if applied else None,
        "patch_proposals": proposed, "successful_patch_count": len(applied),
        "not_found_observations": missing, "not_found_status": "observed" if any(r["status"] == "observed" for r in missing) else "not_observed",
        "Verify": audit["latest_source_Verify"] or "not_run", "independent_Review": audit["reviews"] or "not_run",
        "capacity_certificates": [{k: c[k] for k in ("artifact_id", "source_hash", "status", "total_bytes_bound", "remaining_output_bytes")} for c in audit["capacity_certificates"]],
        "exact_output_billing_verified": audit["exact_output_billing_verified"],
        "metric_limits": "Delivery means bytes present in a request with a preserved real response, not hidden model comprehension. Complete JSON-encoded source characters in delivered receipt pages are mapped to source offsets separately. Read ranges are grouped by output source hash/path/file hash. No unobserved metric is inferred as success."}
    ensure_no_sensitive_data(report, context="Plan15 actual metrics")
    report["report_hash"] = content_hash(report)
    with (ROOT / "coding-metrics.json").open("x") as stream: stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in ("model_requests_started", "successful_source_read_receipts", "effective_source_read_calls_with_new_bytes",
        "unique_source_bytes_observed_by_tools", "unique_source_bytes_delivered_in_confirmed_real_prompts", "exact_duplicate_source_receipts",
        "first_patch_proposal_round", "first_patch_application_round", "read_artifact_requested_calls", "successful_patch_count", "report_hash")}))


if __name__ == "__main__": main()
