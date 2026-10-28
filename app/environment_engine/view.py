"""Safe API projection. Stage completion always refers to current evidence."""
from __future__ import annotations


def environment_view(wb, task_id: str, selected: dict, baseline, *, prepared: bool, review_status: str = "not_run") -> dict | None:
    if not selected.get("environment_engine"):
        return None
    contract = selected.get("verification_contract", {})
    value = wb.read(baseline.payload["result_hash"]) if baseline else {}
    current = bool(value.get("environment_contract_hash") == selected.get("verification_contract_hash")
        and value.get("version") == "execution-baseline-v2")
    receipt = wb.read(value["preparation_receipt_hash"]) if current else None
    verdict = value.get("baseline_verdict") if current else None
    coding = wb.latest(task_id, "agent_v2_session")
    coding_current = bool(current and coding and coding.payload.get("attempt_id") == baseline.payload["attempt_id"])
    return {"version": "environment-engine-view-v1", "contract_hash": selected.get("verification_contract_hash"),
        "stages": {"discovery": "recorded" if contract else "unverified",
            "resolution": contract.get("admission", "unverified"),
            "preparation": "prepared" if prepared and receipt and receipt["status"] == "prepared" else "unverified",
            "entrypoints": "proven" if receipt and receipt["status"] == "prepared" else "unverified",
            "baseline": verdict["outcome"] if verdict else "unverified",
            "coding": "started" if coding_current else "not_started",
            "review": review_status if current and review_status != "not_run" else "unverified"},
        "gaps": contract.get("unresolved", []), "commands": contract.get("commands", []),
        "executables": receipt["executables"] if receipt else [],
        "baseline_reason_codes": verdict.get("reason_codes", []) if verdict else [],
        "evidence_refs": {k: value.get(k) if current else None for k in (
            "environment_contract_hash", "preparation_receipt_hash", "entrypoint_evidence_hash")}}
