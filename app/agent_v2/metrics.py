"""Journey evidence is separate from job, model, check and review success."""
from __future__ import annotations

from app.workbench import Workbench


def journey_metrics(wb: Workbench, task_id: str) -> dict:
    events = wb.history(task_id)
    origin = next((e for e in events if e.kind == "native_journey_started" and e.actor_id == "api"), None)
    interventions = {e.payload["source_id"] for e in events if e.kind == "agent_maintenance_recorded"}
    interventions.update(e.id for e in events if e.kind in {
        "proposal_corrected", "runtime_rebaseline_requested", "preparation_runtime_retry_requested"})
    starts = [e for e in events if e.kind == "agent_v2_model_started"]
    results = [wb.read(e.payload["artifact_id"]) for e in events if e.kind == "agent_v2_tool_result"]
    real_model = any(e.payload.get("transport") == "credential_gateway" for e in starts)
    real_tools = any(r["data"].get("execution_backend") == "docker" for r in results)
    accepted = next((e for e in reversed(events) if e.kind == "execution_accepted" and e.actor_id == "local-user"), None)
    ready = next((e for e in reversed(events) if e.kind == "awaiting_acceptance"), None)
    first_plan = next((e for e in events if e.kind == "plan_bound"), None)
    from app.execution_acceptance import acceptance_view
    current_acceptance = acceptance_view(wb, task_id)
    # A source marker alone proves neither independence nor success. Completion
    # additionally requires current acceptance, review and real runtime evidence.
    complete = bool(origin and first_plan and origin.sequence < first_plan.sequence
        and real_model and real_tools and not interventions and accepted and ready
        and current_acceptance["available"] and current_acceptance["accepted"]
        and accepted.payload.get("review_hash") == ready.payload.get("review_hash"))
    checks = [r for r in results if r["data"].get("tool") == "run_approved_checks"]
    return {"native_journey_started": int(origin is not None), "native_journey_completed": int(complete),
        "operator_interventions": len(interventions), "model_call_count": len(starts),
        "tool_schema_rejection_count": sum(r.get("failure", {}).get("safe_reason_code") == "agent_tool_schema_invalid"
            for r in results if r.get("failure")),
        "repair_rounds": sum(e.kind == "agent_v2_repair_started" for e in events),
        "environment_blocks": sum(r.get("failure", {}).get("category") == "environment" for r in results if r.get("failure")),
        "baseline_blocks": sum(e.kind == "automation_blocked" and e.payload.get("reason_code") == "agent_baseline_blocked" for e in events),
        "actual_tests_executed": None,
        "verified_checks_passed": sum(r["data"].get("actual_checks_started", 0) for r in checks if r["data"].get("outcome") == "passed"),
        "false_success_count": None, "first_attempt_pass_rate": None, "median_token_cost": None,
        "measurement_note": "Unknown test counts/costs and unmeasured rates remain null; check-process counts are not test counts."}
