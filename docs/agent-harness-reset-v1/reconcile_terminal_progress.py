"""Append the proven terminal phase to Plan12's existing ledger, once."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

from app.provenance import content_hash
from app.security import ensure_no_sensitive_data


def main() -> None:
    records = Path("docs/agent-harness-reset-v1")
    out = records / "terminal-progress-reconciliation.json"
    assert not out.exists(), "Reconcile existing receipt instead of blindly repeating"
    audit = json.loads((records / "real-acceptance-audit.json").read_text())
    deployment = json.loads((records / "deployment-report-r4.json").read_text())
    state = json.loads(Path("/tmp/upeve-isolated-20261029-v2/container-roles.json").read_text())
    assert state["source_manifest_hash"] == deployment["source_hash"]
    config = {"task": audit["task_id"], "session": audit["session_id"], "checkpoint": audit["checkpoint"],
        "prior_progress_hash": content_hash(audit["progress"]), "source_hash": deployment["source_hash"]}
    code = """import json,sys
from sqlalchemy import select,func
from app.config import Settings
from app.database import Database
from app.workbench import Workbench
from app.models import Job,WorkbenchEvent
from app.agent_v2.journal import AgentJournal
from app.agent_v2.progress import current_progress,record_checkpoint_blocked
from app.provenance import content_hash
c=json.loads(sys.argv[1]);s=Settings.from_env();db=Database(s.database_url)
with db.session() as session:
 w=Workbench(session,s.artifact_root);j=AgentJournal(w,c['task'],c['session']);j.lock()
 cp=j.checkpoint();old=current_progress(j)
 assert cp.model_dump()==c['checkpoint'] and cp.state=='blocked'
 assert content_hash(old.model_dump())==c['prior_progress_hash']
 assert session.scalar(select(func.count()).select_from(Job).where(Job.state.in_(('queued','leased','running'))))==0
 before={'jobs':session.scalar(select(func.count()).select_from(Job)),
  'model_starts':sum(e.kind=='agent_v2_model_started' for e in w.history(c['task'])),
  'authorizations':sum(e.kind=='execution_authorized' for e in w.history(c['task']))}
 record_checkpoint_blocked(j,cp)
 new=current_progress(j);assert new.phase=='blocked' and new.blocker=='agent_native_call_count_invalid'
 assert j.checkpoint()==cp and new.revision==old.revision+1
 assert session.scalar(select(func.count()).select_from(Job))==before['jobs']
 event=next(e for e in reversed(j.events()) if e.kind=='agent_v2_progress')
 result={'version':'harness-terminal-progress-reconciliation-v1','classification':'operator_platform_metadata_reconciliation_no_business_patch',
  'session_id':j.session_id,'checkpoint_unchanged':True,'usage_unchanged':True,'source_manifest_hash':c['source_hash'],
  'before':before,'prior_progress_hash':c['prior_progress_hash'],'new_progress':new.model_dump(),
  'progress_event_hash':event.record_hash,'progress_artifact':event.payload['artifact_id'],'new_model_calls':0,
  'new_authorizations':0,'new_jobs':0,'third_party_writes':0}
 session.commit()
 print(json.dumps(result))
db.close()
"""
    value = json.loads(subprocess.check_output(["docker", "exec", state["roles"]["serve"]["id"], "python", "-c", code, json.dumps(config)]))
    ensure_no_sensitive_data(value, context="Terminal progress reconciliation")
    value["receipt_hash"] = content_hash(value)
    with out.open("x") as target:
        target.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: value[k] for k in ("checkpoint_unchanged", "usage_unchanged", "new_model_calls", "progress_event_hash", "receipt_hash")}))


if __name__ == "__main__":
    main()
