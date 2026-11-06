"""Export exact registered JSON evidence bytes after the stopped Plan14 run."""
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
from app.security import ensure_no_sensitive_data

ROOT = Path(__file__).resolve().parent
PREP = ROOT.parent / "plan14-preparation"
sys.path.insert(0, str(PREP))
from prepare_plan import docker_read


def main() -> None:
    roles = json.loads(Path("/tmp/upeve-isolated-20261029-v2/container-roles.json").read_text())
    container = roles["roles"]["serve"]["id"]
    code = r'''
import base64,hashlib,json,re
from pathlib import Path
from sqlalchemy import text
from app.config import Settings
from app.database import Database
from app.models import Artifact
from app.security import ensure_no_sensitive_data
from app.workbench import Workbench
s=Settings.from_env();db=Database(s.database_url)
with db.session() as session:
 session.execute(text('BEGIN'));session.execute(text('PRAGMA query_only=ON'))
 wb=Workbench(session,s.artifact_root);task='f0e017fa-d2a6-4eb8-8a94-32bb1d29586c'
 events=[e for e in wb.history(task) if e.sequence>2017];identifiers=set()
 def visit(value):
  if isinstance(value,str) and re.fullmatch('[a-f0-9]{64}',value):identifiers.add(value)
  elif isinstance(value,dict):
   for v in value.values():visit(v)
  elif isinstance(value,list):
   for v in value:visit(v)
 for e in events:visit(e.payload)
 artifacts=[]
 for aid in sorted(identifiers):
  item=session.get(Artifact,aid)
  if item is None or item.media_type!='application/json':continue
  raw=(Path(s.artifact_root)/item.storage_key).read_bytes();assert hashlib.sha256(raw).hexdigest()==aid
  ensure_no_sensitive_data(raw,context='exported Plan14 JSON evidence')
  artifacts.append({'id':aid,'size_bytes':len(raw),'media_type':item.media_type,'base64':base64.b64encode(raw).decode()})
 print(json.dumps({'events':[{'sequence':e.sequence,'kind':e.kind,'record_hash':e.record_hash,'job_id':e.job_id,'payload':e.payload} for e in events],'artifacts':artifacts}))
db.close()
'''
    result = subprocess.run(["docker", "exec", "-i", container, "python", "-"], input=code,
        text=True, capture_output=True, check=True)
    payload = json.loads(result.stdout)
    directory = ROOT / "artifacts"
    directory.mkdir(exist_ok=True)
    exported = []
    for item in payload["artifacts"]:
        raw = base64.b64decode(item["base64"], validate=True)
        assert hashlib.sha256(raw).hexdigest() == item["id"]
        ensure_no_sensitive_data(raw, context="exported immutable evidence")
        with (directory / (item["id"] + ".json")).open("xb") as target:
            target.write(raw)
        exported.append({k: item[k] for k in ("id", "size_bytes", "media_type")})
    before = json.loads((ROOT / "before-dispatch-state.json").read_text())
    after = docker_read(container)
    new_model_starts = after["counts"]["model_starts"] - before["counts"]["model_starts"]
    assert 0 < new_model_starts <= 24
    assert after["counts"]["all_active_jobs"] == 0
    assert after["counts"]["execution_authorizations"] == before["counts"]["execution_authorizations"] + 1
    assert after["counts"]["model_starts"] == before["counts"]["model_starts"] + new_model_starts
    assert 1 <= len(after["checkpoints"].keys() - before["checkpoints"].keys()) <= 3
    for sid, checkpoint in before["checkpoints"].items():
        assert after["checkpoints"][sid] == checkpoint
    assert after["history"][:len(before["history"])] == before["history"]
    assert after["tables"]["plan_versions"] == before["tables"]["plan_versions"]
    assert after["tables"]["model_config_versions"] == before["tables"]["model_config_versions"]
    rows_missing = {}
    for name, table in before["tables"].items():
        missing = Counter(table["row_hashes"]) - Counter(after["tables"][name]["row_hashes"])
        if missing:
            rows_missing[name] = sum(missing.values())
    immutable_tables = ("plan_versions", "plan_approvals", "workbench_events", "contribution_task_state_versions")
    assert not any(name in rows_missing for name in immutable_tables)
    index = {"version": "approved-missing-plan14-evidence-export-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
        "events": payload["events"], "exported_artifacts": exported, "counts_after": after["counts"],
        "all_old_checkpoints_and_event_prefix_unchanged": True, "plan_versions_and_model_config_unchanged": True,
        "preexisting_mutable_rows_updated_by_authorized_run": rows_missing,
        "new_execution_authorizations": 1, "new_model_starts": new_model_starts,
        "new_plans_created": 0, "credential_scan": "passed", "third_party_writes": 0}
    ensure_no_sensitive_data(index, context="Plan14 exported evidence index")
    index["report_hash"] = content_hash(index)
    with (ROOT / "evidence-index.json").open("x") as target:
        target.write(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
    with (ROOT / "after-terminal-state.json").open("x") as target:
        target.write(json.dumps(after, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"exported_JSON_artifacts": len(exported), "events": len(payload["events"]),
        "counts": after["counts"], "preexisting_mutable_rows_updated": rows_missing, "report_hash": index["report_hash"]}))


if __name__ == "__main__":
    main()
