"""Calculate the exact stopped batch admission from saved evidence; no execution."""
from pathlib import Path
import json

from app.agent_v2.contracts import ReadFile
from app.provenance import content_hash
from app.security import ensure_no_sensitive_data

ROOT = Path(__file__).resolve().parent


def main() -> None:
    audit = json.loads((ROOT / "final-audit.json").read_text())
    index = json.loads((ROOT / "evidence-index.json").read_text())
    preview = json.loads((ROOT.parent / "plan14-preparation/execution-preview.json").read_text())
    events = index["events"]
    rejection, = [e for e in events if e["kind"] == "agent_v2_model_rejected"]
    rid = rejection["payload"]["response_artifact"]
    raw = json.loads((ROOT / "artifacts" / (rid + ".json")).read_text())
    calls = raw["choices"][0]["tool_calls"]; assert len(calls) == 8
    args = [ReadFile.model_validate(json.loads(c["function"]["arguments"])) for c in calls]
    bound = sum(6 * a.max_bytes + 8192 for a in args)
    remaining = rejection["payload"]["failure"]["remaining_budget"]["output_bytes"]
    assert bound == 833536 and remaining == 464474 and bound > remaining
    response_event, = [e for e in events if e["kind"] == "agent_v2_model_response" and e["payload"]["artifact_id"] == rid]
    assert not any(e["kind"] in {"agent_v2_tool_started", "agent_v2_read_batch", "agent_v2_read_batch_preflight"}
        and e["sequence"] > response_event["sequence"] for e in events)
    cp = next(e for e in reversed(events[:events.index(rejection)]) if e["kind"] == "agent_v2_checkpoint")["payload"]["checkpoint"]
    assert cp["state"] == "model" and cp["usage"]["output_bytes"] == 35526
    assert preview["additional_run_limits_pending_consent"]["output_bytes"] - cp["usage"]["output_bytes"] == remaining
    actual = {"input_tokens": sum(m["usage"]["prompt_tokens"] for m in audit["model_responses"]),
        "output_tokens": sum(m["usage"]["completion_tokens"] for m in audit["model_responses"])}
    assert actual == {"input_tokens": 24500, "output_tokens": 2665}
    value = {"version": "plan14-read-batch-output-admission-diagnostic-v1",
        "classification": "read_only_saved_real_evidence_and_exact_frozen_source_analysis",
        "response_artifact": rid, "rejection_sequence": rejection["sequence"], "model_job_id": rejection["job_id"],
        "reason_code": "agent_read_batch_budget", "blocking_stage": "investigation_before_batch_creation_and_OCI_preflight",
        "original_call_count": len(calls), "ordered_calls": [{"index": i, "id": c["id"], "name": c["function"]["name"],
            "raw_arguments": c["function"]["arguments"], "normalized_arguments": a.model_dump()}
            for i, (c, a) in enumerate(zip(calls, args, strict=True))],
        "formula": "sum(6 * normalized.max_bytes + 8192) for all original calls",
        "formula_source": "app/agent_v2/read_batches.py:155-159", "admission_bound_bytes": bound,
        "remaining_output_bytes_at_admission": remaining, "bound_excess_bytes": bound - remaining,
        "five_maximum_size_reads_bound": 5 * (6 * 16000 + 8192), "session_output_bytes_cap": 500000,
        "original_batch_max": 8, "execution_sub_batch_max": 4,
        "actual_calls_executed_in_rejected_batch": 0, "reads_reserved_for_rejected_batch": 0,
        "child_batches_created": 0, "OCI_preflight_dispatched_for_rejected_batch": False,
        "raw_response_persisted_before_rejection": True, "provider_reported_actual_usage": actual,
        "charged_checkpoint_usage": audit["checkpoint"]["usage"], "failed_turn_reservations_not_refunded": True,
        "duplicate_reuse": "First four paths repeat offset0 but max_bytes changes4096 to16000; exact arguments differ. Last four are new read requests.",
        "not_found_coverage": "not_observed_no_missing_path_request",
        "security_assessment": "Conservative output admission failed closed. No OCI path/hash/security check was bypassed. This run does not prove later batch path admission or missing-file handling.",
        "root_cause": "Whole-batch worst-case escaped receipt bound uses requested max_bytes before actual source range is known. Eight schema-valid maximum-size reads exceed remaining output capacity; this branch terminates immediately without bounded smaller-request feedback.",
        "follow_up": "Separate authorization is needed to review minimal conservative output accounting and bounded correction offline without raising caps, omitting whole-batch safety checks, changing IDs/order or buying another paid round.",
        "runtime_source_unchanged": True, "terminal_recovery": "Do not resume Plan14 or reset/refund its usage. Any future paid acceptance requires a new exact authorization."}
    ensure_no_sensitive_data(value, context="Plan14 admission diagnostic")
    value["report_hash"] = content_hash(value)
    with (ROOT / "admission-diagnostic.json").open("x") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"bound": bound, "remaining": remaining, "actual_usage": actual, "diagnostic_hash": value["report_hash"]}))


if __name__ == "__main__":
    main()
