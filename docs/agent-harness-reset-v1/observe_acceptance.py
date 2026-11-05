"""Read-only compact observation of the authorized Plan12 acceptance run."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

import httpx

from app.security import ensure_no_sensitive_data

TASK = "f0e017fa-d2a6-4eb8-8a94-32bb1d29586c"
RECORDS = Path("docs/agent-harness-reset-v1")


def main() -> None:
    state = json.loads(Path("/tmp/upeve-isolated-20261029-v2/container-roles.json").read_text())
    code = """import sqlite3,json,sys
db=sqlite3.connect('file:/data/state.db?mode=ro',uri=True)
events=[]
keys={'run_id','attempt_id','stage','status','state','code','reason','error_code','artifact_id','name','failure','response_artifact','response_artifact_id','session_id','usage','outcome','finish_reason'}
for s,k,j,p,h in db.execute('select sequence,kind,job_id,payload,record_hash from workbench_events where task_id=? and sequence>1786 order by sequence',(sys.argv[1],)):
 p=json.loads(p);events.append({'sequence':s,'kind':k,'job_id':j,'record_hash':h,'payload':{k:v for k,v in p.items() if k in keys}})
jobs=[dict(zip(('id','kind','state','attempt_count','error_code','started_at','heartbeat_at','progress_message'),r)) for r in db.execute("select id,kind,state,attempt_count,error_code,started_at,heartbeat_at,progress_message from jobs where json_extract(payload,'$.task_id')=? and created_at>='2026-11-04' order by created_at",(sys.argv[1],))]
print(json.dumps({'events':events,'jobs':jobs}))
"""
    db = json.loads(subprocess.check_output(["docker", "exec", state["roles"]["serve"]["id"], "python", "-c", code, TASK]))
    with httpx.Client(base_url="http://127.0.0.1:18090", timeout=30, trust_env=False) as client:
        response = client.get(f"/api/v1/tasks/{TASK}/workbench")
        response.raise_for_status()
        view = response.json()
    execution = view["execution"]
    native = execution.get("agent_runtime_v2") or {}
    value = {"recorded_at": datetime.now(timezone.utc).isoformat(), "state": view["state"], "busy": view["busy"],
        "run_id": execution.get("run_id"), "baseline": execution.get("baseline"), "runtime": execution.get("runtime"),
        "blocked": execution.get("blocked"), "last_attempt": (execution.get("attempts") or [None])[-1],
        "checkpoint": native.get("checkpoint"), "remaining_budget": native.get("remaining_budget"),
        "implementation_progress": native.get("implementation_progress"), "verification": execution.get("verification"),
        "acceptance": execution.get("acceptance"), "planning_budget": execution.get("planning_budget"), **db}
    ensure_no_sensitive_data(value, context="Harness live observation")
    (RECORDS / "real-acceptance-latest.json").write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    compact = {k: v for k, v in value.items() if k not in ("events", "baseline", "acceptance", "planning_budget", "implementation_progress")}
    progress = value["implementation_progress"]
    compact["progress"] = {k: progress[k] for k in ("revision", "phase", "no_progress_streak", "blocker", "next_action")} if progress else None
    compact["baseline"] = value["baseline"]
    compact["jobs"] = [j for j in value["jobs"] if j["state"] in ("queued", "leased", "running")] + value["jobs"][-2:]
    compact["events_tail"] = value["events"][-6:]
    print(json.dumps(compact, ensure_ascii=False))


if __name__ == "__main__":
    main()
