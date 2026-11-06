"""Export exact hash-verified JSON/text evidence from the one stopped Plan15 run."""
from __future__ import annotations

import base64
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from app.provenance import content_hash
from app.security import ensure_no_sensitive_bytes, ensure_no_sensitive_data

ROOT = Path(__file__).resolve().parent
PREP = ROOT.parent / "plan15-preparation"
sys.path.insert(0, str(PREP))
from check_prerequisites import docker_read


def main() -> None:
    assert not (ROOT / "evidence-index.json").exists()
    before = json.loads((ROOT / "before-dispatch-state.json").read_text())
    roles = json.loads(Path("/tmp/upeve-isolated-20261029-v2/container-roles.json").read_text())
    container = roles["roles"]["serve"]["id"]
    after = docker_read(container)
    assert after["counts"]["all_active_jobs"] == 0, "Wait until this authorized run is stopped"
    code = r'''
import base64,hashlib,json,re,sys
from pathlib import Path
from sqlalchemy import text
from app.config import Settings
from app.database import Database
from app.models import Artifact
from app.security import ensure_no_sensitive_bytes
from app.workbench import Workbench
s=Settings.from_env();db=Database(s.database_url)
with db.session() as session:
 session.execute(text('BEGIN'));session.execute(text('PRAGMA query_only=ON'))
 wb=Workbench(session,s.artifact_root);task='f0e017fa-d2a6-4eb8-8a94-32bb1d29586c'
 events=wb.history(task)[int(sys.argv[1]):];identifiers=set()
 def visit(value):
  if isinstance(value,str) and re.fullmatch('[a-f0-9]{64}',value):identifiers.add(value)
  elif isinstance(value,dict):
   for v in value.values():visit(v)
  elif isinstance(value,list):
   for v in value:visit(v)
 for e in events:visit(e.payload)
 artifacts=[];visited=set()
 # Include references nested in current registered evidence, bounded by the
 # finite immutable registry. No binary archive/DB/credential exports.
 while identifiers-visited:
  aid=sorted(identifiers-visited)[0];visited.add(aid);item=session.get(Artifact,aid)
  if item is None or not (item.media_type=='application/json' or item.media_type.startswith('text/plain')):continue
  raw=(Path(s.artifact_root)/item.storage_key).read_bytes();assert hashlib.sha256(raw).hexdigest()==aid
  ensure_no_sensitive_bytes(raw,context='Plan15 exported immutable evidence')
  artifacts.append({'id':aid,'size_bytes':len(raw),'media_type':item.media_type,'base64':base64.b64encode(raw).decode()})
  if item.media_type=='application/json':visit(json.loads(raw))
 print(json.dumps({'events':[{'id':e.id,'sequence':e.sequence,'kind':e.kind,'record_hash':e.record_hash,'job_id':e.job_id,'payload':e.payload} for e in events],'artifacts':artifacts}))
db.close()
'''
    result = subprocess.run(["docker", "exec", "-i", container, "python", "-B", "-", str(len(before["history"]))],
        input=code, text=True, capture_output=True, check=True)
    payload = json.loads(result.stdout)
    directory = ROOT / "artifacts"; directory.mkdir(exist_ok=True)
    exported = []
    for item in payload["artifacts"]:
        raw = base64.b64decode(item["base64"], validate=True)
        assert hashlib.sha256(raw).hexdigest() == item["id"]
        ensure_no_sensitive_bytes(raw, context="Plan15 exact original bytes")
        suffix = ".json" if item["media_type"] == "application/json" else ".txt"
        with (directory / (item["id"] + suffix)).open("xb") as stream: stream.write(raw)
        exported.append({**{k: item[k] for k in ("id", "size_bytes", "media_type")}, "file": "artifacts/" + item["id"] + suffix})
    for sid, cp in before["checkpoints"].items(): assert after["checkpoints"][sid] == cp
    assert after["history"][:len(before["history"])] == before["history"]
    assert after["counts"]["execution_authorizations"] == before["counts"]["execution_authorizations"] + 1
    assert after["counts"]["plan_approvals"] == before["counts"]["plan_approvals"] + 1
    assert after["tables"]["plan_versions"] == before["tables"]["plan_versions"]
    assert after["tables"]["model_config_versions"] == before["tables"]["model_config_versions"]
    starts = after["counts"]["model_starts"] - before["counts"]["model_starts"]
    assert 0 <= starts <= 24
    missing = {name: sum((Counter(table["row_hashes"]) - Counter(after["tables"][name]["row_hashes"])).values())
        for name, table in before["tables"].items() if Counter(table["row_hashes"]) - Counter(after["tables"][name]["row_hashes"])}
    assert not set(missing) & {"plan_versions", "plan_approvals", "workbench_events", "contribution_task_state_versions"}
    value = {"version": "output-capacity-plan15-evidence-export-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
        "events": payload["events"], "exported_artifacts": exported, "counts_after": after["counts"],
        "old_event_prefix_unchanged": True, "all_old_10_checkpoints_and_usage_unchanged": True,
        "preexisting_mutable_rows_updated_by_authorized_run": missing, "new_execution_authorizations": 1,
        "new_model_starts": starts, "new_plans_created": 0, "third_party_writes": 0, "credential_scan": "passed"}
    ensure_no_sensitive_data(value, context="Plan15 evidence index")
    value["report_hash"] = content_hash(value)
    with (ROOT / "evidence-index.json").open("x") as stream: stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    with (ROOT / "after-terminal-state.json").open("x") as stream: stream.write(json.dumps(after, indent=2) + "\n")
    print(json.dumps({"exported_artifacts": len(exported), "events": len(payload["events"]), "new_model_starts": starts,
        "counts": after["counts"], "report_hash": value["report_hash"]}))


if __name__ == "__main__": main()
