"""Read-only deployment checks. Never print response bodies, logs or secrets."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "--env-file", ".env", "-f",
           "docs/deployment/compose.yaml", "--profile", "nvidia", "--profile", "sandbox"]


def call(argv: list[str], *, source: str | None = None) -> str:
    result = subprocess.run(argv, cwd=ROOT, input=source, capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise SystemExit("Deployment check command failed; raw output suppressed")
    return result.stdout


def main() -> None:
    containers = json.loads(call(["docker", "inspect", "contribos-local-api-1",
        "contribos-model-gateway", "contribos-local-worker-1",
        "contribos-local-provider-worker-1", "contribos-local-sandbox-worker-1"]))
    credentials = set()
    for container in containers:
        for entry in container["Config"]["Env"]:
            name, _, value = entry.partition("=")
            if value and (name.endswith("_KEY") or name.endswith("_TOKEN")):
                credentials.add(value)
    # Host-side comparison keeps the actual NVIDIA key out of the API trust domain.
    dump = call(COMPOSE + ["exec", "-T", "api", "python", "-c",
        'import sqlite3; c=sqlite3.connect("file:/data/contribos.db?mode=ro",uri=True); print("\\n".join(c.iterdump()))'])
    assert not any(secret in dump for secret in credentials), "Credential found in database (value suppressed)"
    logs = call(COMPOSE + ["logs", "--no-color", "--since", "10m", "--tail", "250",
        "api", "model-gateway", "worker", "provider-worker", "sandbox-worker"])
    assert not any(secret in logs for secret in credentials), "Credential found in logs (value suppressed)"
    assert not any(marker in logs for marker in ("Traceback", "ERROR:")), "Service errors detected; raw output suppressed"
    source = '''
import asyncio, hashlib, json, sqlite3, urllib.request
from pathlib import Path
from app.config import Settings
from app.model_settings_api import gateway_manage
c=sqlite3.connect("file:/data/contribos.db?mode=ro",uri=True)
assert c.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
assert not c.execute("PRAGMA foreign_key_check").fetchall()
if AFTER_ERASURE:
    backup=None
    tables=("opportunities","analysis_versions","contribution_tasks","plan_versions","execution_attempts","review_runs","draft_pull_requests","audit_events")
    records={table:{"count":c.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]} for table in tables}
    assert all(records[table]["count"]==0 for table in tables[2:])
    assert not list(Path("/data/backups").glob("pre-studio-*/contribos.db"))
else:
    backup=sorted(Path("/data/backups").glob("pre-studio-*/manifest.json"))[-1]
    records=json.loads(backup.read_text())
    for table, expected in records.items():
        rows=c.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall()
        assert len(rows)==expected["count"]
        assert hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()==expected["hash"]
settings=Settings.from_env()
assert settings.model_gateway_management_key
assert asyncio.run(gateway_manage(settings,"has",{"credential_ref":"env-nvidia"}))["configured"]
responses={}
for path in ("/health","/api/v1/meta","/api/v1/model-settings","/api/v1/tasks","/api/v1/tasks?archived=true","/openapi.json"):
    with urllib.request.urlopen("http://127.0.0.1:8000"+path,timeout=10) as r:
        responses[path]=json.load(r)
configuration=responses["/api/v1/model-settings"]
assert configuration["credential_management_available"]
assert len(configuration["profiles"])>=3
for task in responses["/api/v1/tasks"]:
    with urllib.request.urlopen("http://127.0.0.1:8000/api/v1/tasks/"+task["id"]+"/workbench",timeout=10) as r:
        assert json.load(r)["planner_available"]
with urllib.request.urlopen("http://127.0.0.1:8000/",timeout=10) as r:
    html=r.read().decode()
    assert "studio.css" in html and "show-archived-tasks" in html
    assert "execution-changes-v1" in html
    assert "execution-changes.css" in html
    assert "task-erasure-v1" in html
assert c.execute("SELECT revision FROM _schema_migrations ORDER BY revision DESC LIMIT 1").fetchone()[0]=="0032_minimax_reviews"
for path, method in (("/api/v1/tasks/{task_id}/archive","post"),
                     ("/api/v1/tasks/{task_id}/restore","post"),
                     ("/api/v1/tasks/{task_id}","delete")):
    assert method in responses["/openapi.json"]["paths"][path]
assert all(task["visibility"]=="active" for task in responses["/api/v1/tasks"])
assert all(task["visibility"] in {"archived","deleted"} for task in responses["/api/v1/tasks?archived=true"])
print(json.dumps({"schema":c.execute("SELECT revision FROM _schema_migrations ORDER BY revision DESC LIMIT 1").fetchone()[0],
"counts":{k:v["count"] for k,v in records.items()},"backup":str(backup.parent) if backup else None,
"gateway_management":True,"profiles":len(configuration["profiles"]),"task_endpoints":len(responses["/api/v1/tasks"]),
"archive_restore_delete_routes":True,"archived_tasks":len(responses["/api/v1/tasks?archived=true"])}))
'''
    after_erasure = "--after-erasure" in sys.argv
    evidence = json.loads(call(COMPOSE + ["exec", "-T", "api", "python", "-"], source=source.replace("AFTER_ERASURE", repr(after_erasure))))
    evidence["credentials_absent_from_database_and_logs"] = True
    if not after_erasure:
        evidence["old_row_hashes_unchanged"] = True
    evidence["services"] = {c["Name"]: c["State"]["Status"] for c in containers}
    print(json.dumps(evidence, ensure_ascii=False))


if __name__ == "__main__":
    main()
