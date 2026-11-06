"""Offline immutable-evidence analysis; no provider, live DB or execution API.

Run from the repository: PYTHONPATH=. .venv/bin/python <this file>
"""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

from app.agent_v2.coding_context import observe
from app.agent_v2.native_tools import native_input_bound, native_model_transport, project_native_prompt
from app.provenance import content_hash
from app.security import ensure_no_sensitive_data
from tests.test_coding_loop_recovery import archived_journal

ROOT = Path(__file__).resolve().parent
SAVED = ROOT.parent / "read-batch-scheduler-v1/plan15-execution"


def main() -> None:
    index = json.loads((SAVED / "evidence-index.json").read_text())
    assert content_hash({k: v for k, v in index.items() if k != "report_hash"}) == index["report_hash"]
    files = {r["id"]: SAVED / r["file"] for r in index["exported_artifacts"]}
    for aid, path in files.items():
        assert sha256(path.read_bytes()).hexdigest() == aid
    def read(aid: str) -> dict:
        return json.loads(files[aid].read_bytes())
    metrics = json.loads((SAVED / "coding-metrics.json").read_text())
    assert content_hash({k: v for k, v in metrics.items() if k != "report_hash"}) == metrics["report_hash"]
    events = index["events"]
    starts = [e for e in events if e["kind"] == "agent_v2_model_started"]
    responses = {e["job_id"]: e for e in events if e["kind"] == "agent_v2_model_response"}
    rounds = []
    for ordinal, start in enumerate(starts, 1):
        prompt_id = start["payload"]["prompt_artifact"]
        prompt = read(prompt_id)
        event = responses[start["job_id"]]
        response_id = event["payload"]["artifact_id"]
        response = read(response_id)
        assert response["request_id"] == start["job_id"]
        assert response["binding_hash"] == content_hash(prompt["binding"])
        next_sequence = starts[ordinal]["sequence"] if ordinal < len(starts) else 10**9
        after = [e for e in events if start["sequence"] <= e["sequence"] < next_sequence]
        receipts = [e for e in after if e["kind"] == "agent_v2_tool_result"]
        ledgers = [e for e in after if e["kind"] == "agent_v2_progress"]
        calls = [{"provider_call_id": c["id"], "name": c["function"]["name"],
            "arguments": json.loads(c["function"]["arguments"])} for c in response["choices"][0]["tool_calls"] or []]
        coverage = metrics["prompt_inline_source_coverage"][ordinal - 1]
        row = {"round": ordinal, "prompt_artifact": prompt_id, "response_artifact": response_id,
            "goal": prompt["goal"], "approved_task_input": prompt["approved_task_input"],
            "task_context_artifact": prompt["task_context_artifact"],
            "source_hash": prompt["source_hash"], "input_bound": start["payload"]["input_bound"],
            "output_reservation": start["payload"]["output_reservation"],
            "actual_usage": response["usage"], "finish_reason": response["choices"][0]["finish_reason"],
            "history_index": prompt["history_index"], "history_evidence": prompt.get("history_evidence"),
            "recent_decisions": prompt["recent_decisions"], "progress_seen": prompt["implementation_progress"],
            "protocol_correction_seen": prompt.get("native_protocol_correction"),
            "inline_source_coverage": coverage, "calls": calls,
            "receipts": [{"artifact_id": e["payload"]["artifact_id"], "sequence": e["sequence"],
                "provider_call_id": e["payload"].get("provider_call_id"),
                "status": read(e["payload"]["artifact_id"])["status"]} for e in receipts],
            "source_observations": [r for r in metrics["source_reads"]
                if r["receipt_artifact"] in {e["payload"]["artifact_id"] for e in receipts}],
            "progress_after_round": ledgers[-1]["payload"]["progress"] if ledgers else None}
        journal, old_prompt, loader = archived_journal(ordinal)
        coding, pages = observe(journal)
        row["new_policy_diagnosis_only"] = coding
        if ordinal in (7, 13):
            from types import SimpleNamespace
            old_prompt["feedback"] = [{**loader(r["receipt_artifact"]), "receipt_artifact": r["receipt_artifact"]}
                for r in old_prompt["feedback"]]
            old_prompt["implementation_progress"]["coding"] = coding
            projected = project_native_prompt(old_prompt, SimpleNamespace(model_transport=native_model_transport()),
                task_input=loader(old_prompt["task_context_artifact"]), source_pages=pages,
                history_artifact=old_prompt.get("history_evidence", {}).get("artifact_id"))
            assert native_input_bound(projected) <= 32000
            row["offline_projection_comparison"] = {"classification": "counterfactual_projection_only_no_dispatch",
                "new_input_bound": native_input_bound(projected),
                "new_inline_source_bytes": sum(len(p["text"].encode()) for p in projected["source_working_set"]),
                "current_excerpt_paths": [p["path"] for p in projected["source_working_set"]],
                "note": "Round 13 is diagnostic only; the new 8-round guard would already stop this trace."}
        rounds.append(row)
    report = {"version": "coding-loop-recovery-plan15-trace-v1", "paid_requests": 0,
        "classification": "archived_real_evidence_analysis_and_offline_counterfactual_projection",
        "original_index_hash": index["report_hash"], "original_metrics_hash": metrics["report_hash"],
        "verified_artifacts": len(files), "prompt_count": len(starts), "response_count": len(responses),
        "summary": {k: metrics[k] for k in ("actual_provider_usage", "charged_usage", "successful_source_read_receipts",
            "effective_source_read_calls_with_new_bytes", "unique_source_bytes_observed_by_tools",
            "unique_source_bytes_delivered_in_confirmed_real_prompts", "exact_duplicate_source_receipts",
            "overlapping_source_receipts", "artifact_pagination_recovery", "Verify", "independent_Review")},
        "rounds": rounds}
    ensure_no_sensitive_data(report, context="offline Plan15 trace")
    report["report_hash"] = content_hash(report)
    (ROOT / "plan15-trace.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"rounds": len(rounds), "verified_artifacts": len(files), "report_hash": report["report_hash"],
        "comparisons": [r["offline_projection_comparison"] for r in rounds if "offline_projection_comparison" in r]}))


if __name__ == "__main__":
    main()
