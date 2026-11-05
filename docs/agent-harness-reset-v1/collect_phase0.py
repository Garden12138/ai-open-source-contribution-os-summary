"""Read a frozen Plan11 snapshot; emit derived metadata, never provider reasoning.

Run from the repository root with PYTHONPATH=.; output must be a new file.
This collector does not invoke a model, execute repository code or open live state.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess

from app.provenance import content_hash
from app.security import ensure_no_sensitive_data

SESSION = "session-2dd44f18b8adba684806de67cc01a327"
EVENT_KEYS = ("id", "task_id", "sequence", "kind", "actor_id", "job_id",
              "plan_version_id", "payload_hash", "previous_hash")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def contains_response_envelope(item: object) -> bool:
    if isinstance(item, dict):
        if isinstance(item.get("choices"), list) or isinstance(item.get("tool_calls"), list):
            return True
        return any(contains_response_envelope(child) for child in item.values())
    if isinstance(item, list):
        return any(contains_response_envelope(child) for child in item)
    if isinstance(item, str) and item.lstrip().startswith(("{", "[")):
        try:
            decoded = json.loads(item)
        except ValueError:
            return False
        return contains_response_envelope(decoded)
    return False


def collect(root: Path) -> dict:
    """Validate historical hashes and correlate every accepted read and prompt."""
    db_path = root / "state.db"
    before = sha(db_path)
    with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not list(db.execute("PRAGMA foreign_key_check"))
        artifacts = {}
        raw_candidates = []
        response_envelopes = []
        rows = list(db.execute("SELECT id, storage_key FROM artifacts"))
        for row in rows:
            path = (root / "artifacts" / row["storage_key"]).resolve()
            assert path.is_relative_to((root / "artifacts").resolve())
            raw = path.read_bytes()
            assert hashlib.sha256(raw).hexdigest() == row["id"]
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            ensure_no_sensitive_data(value, context="Phase 0 historical artifact")
            artifacts[row["id"]] = value

            if contains_response_envelope(value):
                response_envelopes.append(row["id"])
            if isinstance(value, dict) and "raw_output" in value:
                raw_candidates.append({"artifact_id": row["id"], "version": value.get("version")})

        registered = {(root / "artifacts" / row["storage_key"]).resolve() for row in rows}
        orphans = []
        for path in sorted((root / "artifacts" / "sha256").rglob("*")):
            if not path.is_file() or path.resolve() in registered:
                continue
            assert sha(path) == path.name
            value = json.loads(path.read_bytes())
            ensure_no_sensitive_data(value, context="Phase 0 unregistered artifact")
            if contains_response_envelope(value):
                response_envelopes.append(path.name)
            orphans.append({"artifact_id": path.name,
                "version": value.get("version") if isinstance(value, dict) else None})

        session_rows = []
        for row in db.execute("SELECT * FROM workbench_events ORDER BY task_id, sequence"):
            event = dict(row)
            event["payload"] = json.loads(event["payload"])
            if event["payload"].get("session_id") == SESSION:
                session_rows.append(event)
        task_id = session_rows[0]["task_id"]
        history = [dict(row) for row in db.execute(
            "SELECT * FROM workbench_events WHERE task_id=? ORDER BY sequence", (task_id,))]
        previous = None
        for number, event in enumerate(history, 1):
            event["payload"] = json.loads(event["payload"])
            assert event["sequence"] == number and event["previous_hash"] == previous
            assert event["payload_hash"] == content_hash(event["payload"])
            assert event["record_hash"] == content_hash({k: event[k] for k in EVENT_KEYS})
            ensure_no_sensitive_data(event["payload"], context="Phase 0 historical event")
            previous = event["record_hash"]
        proposal = json.loads(Path("docs/acceptance/universal-environment-v1/ci-native-plan11-execution-proposal.json").read_text())
        assert [{"id": e["id"], "record_hash": e["record_hash"]} for e in history[:1473]] == proposal["native_journal"]
        spec = artifacts[next(e for e in session_rows if e["kind"] == "agent_v2_session")["payload"]["spec_artifact"]]
        auth = next(e for e in history if e["record_hash"] == spec["binding"]["authorization_hash"])
        assert content_hash({k: auth["payload"][k] for k in ("implementation", "review")}) == spec["binding"]["model_binding_hash"]
        turns = [e for e in session_rows if e["kind"] == "agent_v2_turn"]
        prompts = []
        for event in session_rows:
            if event["kind"] != "agent_v2_model_started":
                continue
            prompt = artifacts[event["payload"]["prompt_artifact"]]
            assert content_hash(prompt["approved_task_input"]) == prompt["task_context_artifact"]
            delivery = []
            for feedback in prompt["feedback"]:
                receipt = artifacts[feedback["receipt_artifact"]]
                assert receipt["call_id"] == feedback["call_id"]
                assert receipt["output_source_hash"] == feedback["output_source_hash"]
                if receipt["data"].get("tool") != "read_file":
                    continue
                data, original = feedback["data"], receipt["data"]
                assert data["offset"] == original["offset"] and data["content_hash"] == original["content_hash"]
                assert original["text"].startswith(data["text"])
                end = data["offset"] + len(data["text"].encode())
                assert data["next_offset"] == (end if end < data["total_bytes"] else None)
                delivery.append({"receipt_artifact": feedback["receipt_artifact"],
                    "delivered_bytes": len(data["text"].encode()), "next_offset": data["next_offset"],
                    "complete_receipt_text": data["text"] == original["text"],
                    "source_hash": feedback["output_source_hash"]})
            turn = next((e for e in turns if e["job_id"] == event["job_id"]), None)
            prompts.append({"sequence": event["sequence"], "job_id": event["job_id"],
                "prompt_artifact": event["payload"]["prompt_artifact"],
                "input_bound": event["payload"]["input_bound"], "transport": event["payload"]["transport"],
                "workspace_state": prompt["workspace_state"], "source_hash": prompt["source_hash"],
                "history_index": prompt["history_index"], "history_evidence": prompt.get("history_evidence"),
                "read_feedback": delivery, "task_input_complete": True,
                "protocol_correction": prompt.get("native_protocol_correction"),
                "accepted_turn": None if turn is None else turn["sequence"]})
        reads, fingerprints, tools = [], Counter(), Counter()
        for index, event in enumerate(session_rows):
            if event["kind"] != "agent_v2_tool_result":
                continue
            tools[event["payload"]["name"]] += 1
            if event["payload"]["name"] != "read_file":
                continue
            cp = next(e["payload"]["checkpoint"] for e in reversed(session_rows[:index])
                      if e["kind"] == "agent_v2_checkpoint")
            call = cp["pending_calls"][0]
            turn = next(e for e in reversed(session_rows[:index]) if e["kind"] == "agent_v2_turn")
            original_call = next(c for c in turn["payload"]["decision"]["calls"]
                if "tool-" + content_hash([turn["job_id"], c["call_id"]])[:40] == call["call_id"])
            aid = event["payload"]["artifact_id"]
            receipt = artifacts[aid]
            assert receipt["call_id"] == call["call_id"] == event["payload"]["call_id"]
            assert receipt["source_hash"] == cp["source_hash"]
            assert receipt["input_hash"] == content_hash({"binding": spec["binding"],
                "source_hash": cp["source_hash"], "call": call})
            args = {"offset": 0, "max_bytes": 4096, "expected_hash": None, **call["arguments"]}
            data = receipt["data"]
            assert data["path"] == args["path"] and data["offset"] == args["offset"]
            assert receipt["status"] == "succeeded" and data["execution_backend"] == "docker"
            key = content_hash({"tool": "read_file", "arguments": args, "source_hash": receipt["source_hash"],
                "file_hash": data["content_hash"], "status": receipt["status"]})
            fingerprints[key] += 1
            refs = []
            for prompt in prompts:
                for f in prompt["read_feedback"]:
                    if f["receipt_artifact"] == aid:
                        assert prompt["sequence"] > event["sequence"]
                        refs.append({"prompt_sequence": prompt["sequence"], "prompt_artifact": prompt["prompt_artifact"], **f})
            reads.append({"sequence": event["sequence"], "event_hash": event["record_hash"],
                "job_id": event["job_id"], "decision_sequence": turn["sequence"],
                "normalized_native_call_id": original_call["call_id"], "platform_call_id": call["call_id"],
                "raw_vendor_call_id": None, "raw_arguments": call["arguments"], "effective_arguments": args,
                "source_hash": receipt["source_hash"], "output_source_hash": receipt["output_source_hash"],
                "file_content_hash": data["content_hash"], "receipt_artifact": aid, "input_hash": receipt["input_hash"],
                "returned_bytes": len(data["text"].encode()), "total_bytes": data["total_bytes"],
                "next_offset": data["next_offset"], "status": receipt["status"], "fingerprint": key,
                "prior_exact_requests": fingerprints[key] - 1, "later_prompt_feedback": refs})
        rejections = [{"sequence": e["sequence"], "event_hash": e["record_hash"], "job_id": e["job_id"],
            "persisted_payload": e["payload"], "raw_response": None, "original_calls": None,
            "all_calls_read_only": "unproven"} for e in session_rows if e["kind"] == "agent_v2_model_rejected"]
        checkpoint = next(e["payload"]["checkpoint"] for e in reversed(session_rows) if e["kind"] == "agent_v2_checkpoint")
    assert sha(db_path) == before
    assert len(reads) == 39 and len(prompts) == 14 and len(turns) == 12 and len(rejections) == 2
    revision = "cf051560b8aa64cd01dd2abd351b7d6710d4d74f"
    for path, expected in proposal["tested_production_files"].items():
        raw = subprocess.check_output(["git", "show", revision + ":" + path])
        assert hashlib.sha256(raw).hexdigest() == expected
    history_files = json.loads(Path("docs/acceptance/universal-environment-v1/e0-baseline.json").read_text())["history_files"]
    assert all(sha(Path(path)) == expected for path, expected in history_files.items())
    candidate = json.loads(Path("docs/acceptance/universal-environment-v1/read-progress-paused-checkpoint.json").read_text())["worktree_files_not_included_in_documentation_commit"]
    assert all(sha(Path(path)) == expected for path, expected in candidate.items())
    return {"version": "agent-harness-reset-phase0-evidence-v1", "status": "partial",
        "gate0": "blocked_missing_original_rejected_provider_calls", "snapshot_root": str(root),
        "snapshot_database_hash": before, "snapshot_database_unchanged": True,
        "historical_engine_revision": revision,
        "session_id": SESSION, "historical_progress_phase": "not_recorded",
        "binding": spec["binding"], "limits": spec["limits"],
        "model_binding": {k: auth["payload"][k] for k in ("implementation", "review")},
        "integrity": {"artifact_hashes_verified": len(rows), "json_artifacts_scanned": len(artifacts),
            "event_chain_verified": len(history), "old_event_prefix_preserved": 1473,
            "foreign_keys": "passed", "credential_scan": "passed",
            "frozen_source_files_verified": len(proposal["tested_production_files"]),
            "historical_files_unchanged": len(history_files), "user_candidate_files_unchanged": len(candidate)},
        "raw_evidence_search": {"scope": "registered and unregistered content-addressed snapshot artifacts, recursively including JSON-valued strings",
            "provider_response_envelope_artifacts": sorted(set(response_envelopes)),
            "unregistered_artifacts_scanned": orphans,
            "other_raw_output_candidates": raw_candidates, "live_gateway_capture": "not_available_not_inferred"},
        "summary": {"models": len(prompts), "decisions": len(turns), "tools": dict(tools),
            "exact_read_fingerprints": len(fingerprints), "exact_duplicate_requests": sum(c - 1 for c in fingerprints.values()),
            "repeated_path_start_offsets": len(reads) - len({(r['effective_arguments']['path'], r['effective_arguments']['offset']) for r in reads}),
            "read_receipts_in_later_prompts": sum(bool(r["later_prompt_feedback"]) for r in reads),
            "read_feedback_entries": sum(len(p["read_feedback"]) for p in prompts),
            "complete_receipt_text_deliveries": sum(f["complete_receipt_text"] for p in prompts for f in p["read_feedback"])},
        "rejections": rejections, "reads": reads, "prompts": prompts, "terminal_checkpoint": checkpoint,
        "inference_limit": "No raw rejected call identities, names, arguments or order survive in this snapshot. Hidden model intent and safe read-only splitting are unproven.",
        "new_model_calls": 0, "business_state_changes": 0, "third_party_writes": 0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = collect(args.snapshot_root)
    ensure_no_sensitive_data(value, context="Phase 0 derived report")
    value["record_hash"] = content_hash(value)
    with args.output.open("x") as out:
        out.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"gate0": value["gate0"], "integrity": value["integrity"], "summary": value["summary"]}))


if __name__ == "__main__":
    main()
