"""Read-only observations of the single authorized Plan15 execution."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

import httpx

from app.provenance import content_hash
from app.security import ensure_no_sensitive_data

ROOT = Path(__file__).resolve().parent
TASK = "f0e017fa-d2a6-4eb8-8a94-32bb1d29586c"


def main() -> None:
    roles = json.loads(Path("/tmp/upeve-isolated-20261029-v2/container-roles.json").read_text())
    code = r'''
import json,sqlite3,sys
from datetime import datetime,timezone
db=sqlite3.connect('file:/data/state.db?mode=ro',uri=True);db.execute('pragma query_only=on')
task=sys.argv[1];events=[];ids=set()
keys={'run_id','attempt_id','stage','status','state','code','reason','reason_code','error_code','artifact_id','name','failure','response_artifact','response_artifact_id','session_id','usage','outcome','finish_reason','batch_artifact','sub_batch_id','index','call_count','reads_reserved','call_ids','receipts','source_hash','actual_receipts','not_executed','unknown_calls','verification','diff_hash','review_hash','result_hash'}
for s,k,j,p,h in db.execute('select sequence,kind,job_id,payload,record_hash from workbench_events where task_id=? and sequence>2094 order by sequence',(task,)):
 p=json.loads(p);events.append({'sequence':s,'kind':k,'job_id':j,'record_hash':h,'payload':{k:v for k,v in p.items() if k in keys}})
 if j:ids.add(j)
jobs=[]
for row in db.execute("select id,kind,state,attempt_count,error_code,started_at,heartbeat_at,progress_message,created_at from jobs where id in (select job_id from workbench_events where task_id=? and sequence>2094) order by created_at",(task,)):
 if row[0] in ids:
  jobs.append(dict(zip(('id','kind','state','attempt_count','error_code','started_at','heartbeat_at','progress_message','created_at'),row)))
print(json.dumps({'events':events,'jobs':jobs,'execution_authorizations':db.execute("select count(*) from workbench_events where task_id=? and kind='execution_authorized'",(task,)).fetchone()[0]}))
'''
    observed = json.loads(subprocess.check_output(["docker", "exec", roles["roles"]["serve"]["id"], "python", "-c", code, TASK]))
    with httpx.Client(base_url="http://127.0.0.1:18090", timeout=30, trust_env=False) as client:
        response = client.get(f"/api/v1/tasks/{TASK}/workbench")
        response.raise_for_status()
        view = response.json()
    execution = view["execution"]
    native = execution.get("agent_runtime_v2") or {}
    progress = native.get("implementation_progress")
    checkpoint = native.get("checkpoint")
    current_root = bool(checkpoint and any(e["kind"] == "agent_v2_session"
        and e["payload"].get("session_id") == checkpoint["session_id"] for e in observed["events"]))
    value = {"version": "approved-missing-plan15-observation-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
        "state": view["state"], "busy": view["busy"], "run_id": execution.get("run_id"),
        "baseline": execution.get("baseline"), "blocked": execution.get("blocked"),
        "last_attempt": (execution.get("attempts") or [None])[-1], "checkpoint": native.get("checkpoint"),
        "remaining_budget": native.get("remaining_budget"), "implementation_progress": progress,
        "native_session_scope": "current_Plan15" if current_root else "historical_not_current_Plan15",
        "verification": execution.get("verification"), "acceptance": execution.get("acceptance"),
        "planning_budget": execution.get("planning_budget"), **observed}
    ensure_no_sensitive_data(value, context="Plan15 live observation")
    value["observation_hash"] = content_hash(value)
    (ROOT / "latest.json").write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    archive = ROOT / "observations"
    archive.mkdir(exist_ok=True)
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ.json")
    with (archive / name).open("x") as target:
        target.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    cp = value["checkpoint"]
    compact = {k: value[k] for k in ("recorded_at", "state", "busy", "run_id", "blocked", "last_attempt", "remaining_budget", "execution_authorizations")}
    compact["checkpoint"] = {k: cp[k] for k in ("session_id", "state", "source_hash", "usage", "revision")} if cp else None
    compact["native_session_scope"] = value["native_session_scope"]
    if not current_root:
        compact["checkpoint"] = None
        compact["remaining_budget"] = "new_native_session_not_started"
    compact["progress"] = {k: progress[k] for k in ("phase", "no_progress_streak", "blocker", "next_action")} if progress else None
    if not current_root:
        compact["progress"] = None
    compact["baseline_summary"] = None if value["baseline"] is None else {k: v for k, v in value["baseline"].items() if k in {"status", "outcome", "summary", "record_hash", "preparation_result_hash", "baseline_result_hash"}}
    compact["jobs"] = [j for j in observed["jobs"] if j["state"] in {"queued", "leased", "running"}] + observed["jobs"][-2:]
    compact["events_tail"] = observed["events"][-5:]
    print(json.dumps(compact, ensure_ascii=False))


if __name__ == "__main__":
    main()
