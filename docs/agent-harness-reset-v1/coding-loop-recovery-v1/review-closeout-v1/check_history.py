"""Read-only history audit in the original service; no execution/deployment API."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess

from app.provenance import content_hash

ROOT = Path(__file__).resolve().parent
PLANS = {"Plan11": "2026fb2d-29f5-4b1d-81b9-a0ba125505bb", "Plan12": "5c6df081-f469-44be-a2de-81fec47b2289",
    "Plan13": "b3c5acdd-bb80-461f-8dd9-5af5ca687280", "Plan14": "e8503bc5-9b61-4e51-95c6-c4eb33bb919e",
    "Plan15": "626be813-5437-4ad3-8a24-e30d12c7b803"}


def main() -> None:
    spec = importlib.util.spec_from_file_location("closeout_compatibility", ROOT.parent / "check_compatibility.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    module.ROOT = ROOT
    module.main()
    os.replace(ROOT / "compatibility-live.json", ROOT / "compatibility-after.json")
    before = json.loads((ROOT / "compatibility-before.json").read_text())
    after = json.loads((ROOT / "compatibility-after.json").read_text())
    assert before == after
    roles = json.loads(Path("/tmp/upeve-isolated-20261029-v2/container-roles.json").read_text())["roles"]
    program = '''
import json,sys
from sqlalchemy import text
from app.config import Settings
from app.database import Database
from app.agent_v2.journal import AgentJournal
from app.workbench import Workbench
s=Settings.from_env();db=Database(s.database_url)
with db.session() as session:
 session.execute(text("BEGIN"));session.execute(text("PRAGMA query_only=ON"))
 wb=Workbench(session,s.artifact_root);task="f0e017fa-d2a6-4eb8-8a94-32bb1d29586c";values={}
 for root in wb.history(task):
  if root.kind!="agent_v2_session":continue
  j=AgentJournal(wb,task,root.payload["session_id"]);spec=j.spec();cp=j.checkpoint()
  values[spec.binding.plan_id]={"session_id":j.session_id,"checkpoint":cp.model_dump(),
   "plan_hash":spec.binding.plan_hash,"authorization_hash":spec.binding.authorization_hash,
   "event_hashes":[e.record_hash for e in j.events()]}
 print(json.dumps({label:values[pid] for label,pid in json.loads(sys.argv[1]).items()}))
db.close()
'''
    plans = json.loads(subprocess.check_output(["docker", "exec", roles["serve"]["id"], "python", "-B", "-c", program, json.dumps(PLANS)]))
    terminal = json.loads((ROOT.parent.parent / "read-batch-scheduler-v1/plan15-execution/after-terminal-state.json").read_text())
    for label, record in plans.items():
        assert record["checkpoint"] == terminal["checkpoints"][record["session_id"]]
        assert record["checkpoint"]["state"] == "blocked"
        record["plan_id"] = PLANS[label]
        record["event_count"] = len(record["event_hashes"])
        record["event_hash_set_hash"] = content_hash(record.pop("event_hashes"))
    report = {"version": "coding-closeout-plan11-15-query-only-v1", "all_45_tables_and_11_checkpoints_unchanged": True,
        "before_after_compatibility_hash": before["report_hash"], "plans": plans,
        "events_approvals_usage_unknown_holds_unchanged": True, "counts": after["counts"],
        "paid_model_calls": 0, "deployments": 0, "restored_terminal_states": 0}
    report["report_hash"] = content_hash(report)
    (ROOT / "history-plan11-15.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"history": "unchanged", "plans": {k: v["checkpoint"]["usage"] for k,v in plans.items()}}))


if __name__ == "__main__": main()
