"""Synthetic DB/authority replay of Plan12–15 saved envelopes; no live writes."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile

from pytest import MonkeyPatch
from app.provenance import content_hash
from app.security import ensure_no_sensitive_data
from app.agent_v2.journal import AgentJournal
from app.workbench import Workbench

ROOT = Path(__file__).resolve().parent


def main() -> None:
    path = ROOT.parent / "read-batch-scheduler-v1/output-capacity-v1/replay.py"
    spec = importlib.util.spec_from_file_location("offline_capacity_replay", path)
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    replay.ROOT = ROOT / "saved-envelope-replay"
    replay.ROOT.mkdir(exist_ok=True)
    reports = []
    def capture(values, label, response_artifact):
        report, _ = replay.snapshot(values, label, response_artifact)
        # Export complete task/history references as well as tool receipts;
        # retaining only the abbreviated model frame is not recoverability.
        with values[0].session() as session:
            j = AgentJournal(Workbench(session, values[1].artifact_root), values[2], values[3])
            prompt = json.loads((replay.ROOT / "artifacts" / (report["prompt_artifact"] + ".json")).read_text())
            refs = {prompt["task_context_artifact"], j.root().payload["spec_artifact"]}
            if prompt.get("history_evidence"):
                refs.add(prompt["history_evidence"]["artifact_id"])
            if prompt.get("source_files"):
                refs.add(prompt["source_files"]["frozen_inventory_artifact"])
            for event in j.events():
                if event.kind == "agent_v2_model_started":
                    refs.add(event.payload["prompt_artifact"])
                    refs.update(event.payload.get("context_artifacts", []))
            for aid in refs:
                assert replay.artifact(j.wb.read(aid)) == aid
        return report
    with tempfile.TemporaryDirectory(prefix="offline-coding-recovery-") as temporary:
        base = Path(temporary)
        for i in range(3):
            values = replay.setup(base / str(i), replay.plan12_response(i), gateway=replay.FrozenGateway())
            report = capture(values, "Plan12-" + str(i + 1), replay.PLAN12["batches"][i]["response_artifact"])
            reports.append(report)
        with MonkeyPatch.context() as patch:
            values = replay.missing_fixture(base / "13", patch, replay.plan13_response())
            report = capture(values, "Plan13", replay.PLAN13["response_artifact"])
            reports.append(report)
        values = replay.setup(base / "14", replay.plan14_response(), gateway=replay.FrozenGateway())
        report = capture(values, "Plan14", replay.SAVED_PATH.stem)
        reports.append(report)
        saved = ROOT.parent / "read-batch-scheduler-v1/plan15-execution"
        index = json.loads((saved / "evidence-index.json").read_text())
        responses = [e for e in index["events"] if e["kind"] == "agent_v2_model_response"]
        for number in (6, 12):
            aid = responses[number - 1]["payload"]["artifact_id"]
            raw = json.loads((saved / "artifacts" / (aid + ".json")).read_text())
            assert content_hash(raw) == aid
            choice = raw["choices"][0]
            wire = {"choices": [{"finish_reason": choice["finish_reason"], "message": {
                "role": choice["role"], "content": None, "tool_calls": choice["tool_calls"]}}], "usage": raw["usage"]}
            values = replay.setup(base / ("15-" + str(number)), wire, gateway=replay.FrozenGateway())
            reports.append(capture(values, "Plan15-round-" + str(number), aid))
    assert [r["child_sizes"] for r in reports] == [[4, 2], [4, 1], [4, 1], [4, 4], [4, 4], [4, 4], [4, 4]]
    assert reports[3]["model_visible_statuses"] == ["succeeded"] * 7 + ["not_found"]
    value = {"version": "coding-recovery-v11-saved-envelopes-v1", "new_paid_requests": 0,
        "new_live_plans_or_approvals": 0, "historical_approval_reused": False,
        "classification": "saved_envelopes_synthetic_authority_deterministic_tools_current_prompt_for",
        "replays": reports}
    ensure_no_sensitive_data(value, context="offline saved-envelope report")
    value["report_hash"] = content_hash(value)
    (ROOT / "saved-envelope-replay.json").write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"replays": len(reports), "child_sizes": [r["child_sizes"] for r in reports],
        "paid_requests": 0, "report_hash": value["report_hash"]}))


if __name__ == "__main__":
    main()
