"""Redeploy only the five owned acceptance roles, preserving all authority/config.

Secrets stay in memory while copying the original Docker configuration; neither
inspect payloads nor environment values are logged or written to this report.
"""
from __future__ import annotations

from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

import httpx

from app.provenance import content_hash
from app.security import ensure_no_sensitive_data

RECORDS = Path("docs/agent-harness-reset-v1")
STATE = Path("/tmp/upeve-isolated-20261029-v2/container-roles.json")
TASK = "f0e017fa-d2a6-4eb8-8a94-32bb1d29586c"


def docker_json(*args: str) -> object:
    return json.loads(subprocess.check_output(["docker", *args]))


def observation(container: str) -> dict:
    code = """import json,sqlite3
from app.provenance import content_hash
db=sqlite3.connect('file:/data/state.db?mode=ro',uri=True)
tables=[x[0] for x in db.execute("select name from sqlite_master where type='table'")]
print(json.dumps({'active':db.execute("select count(*) from jobs where state in ('queued','leased','running')").fetchone()[0],
 'tables':{t:content_hash(sorted(list(db.execute('select * from '+t)),key=str)) for t in tables},
 'migrations':list(db.execute('select * from _schema_migrations order by revision')),
 'integrity':db.execute('pragma integrity_check').fetchone()[0],
 'foreign_keys':list(db.execute('pragma foreign_key_check'))}))
"""
    return json.loads(subprocess.check_output(["docker", "exec", container, "python", "-c", code]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=RECORDS / "source-manifest.json")
    parser.add_argument("--build-report", type=Path, default=RECORDS / "build-report.json")
    parser.add_argument("--output", type=Path, default=RECORDS / "deployment-report.json")
    args = parser.parse_args()
    out = args.output
    if out.exists():
        raise ValueError("Deployment already recorded; reconcile before retry")
    for name in ("harness-final-shard0.xml", "harness-final-shard1.xml", "harness-real-oci.xml"):
        cases = ET.parse(Path("/tmp") / name).findall(".//testcase")
        assert cases and not any(c.find("failure") is not None or c.find("error") is not None for c in cases), name
    manifest = json.loads(args.manifest.read_text())
    if manifest.get("previous_source_hash"):
        delta = Path(manifest["delta_report"])
        assert hashlib.sha256(delta.read_bytes()).hexdigest() == manifest["delta_report_hash"]
        assert not any(c.find("failure") is not None or c.find("error") is not None
            for c in ET.parse(delta).findall(".//testcase"))
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p, h in manifest["files"].items())
    build = json.loads(args.build_report.read_text())
    assert build["source_hash"] == manifest["source_hash"]
    state = json.loads(STATE.read_text())
    assert state["business_volume"] == "upeve-isolated-business-20261103"
    old = {role: docker_json("inspect", value["id"])[0] for role, value in state["roles"].items()}
    assert set(old) == {"serve", "worker", "sandbox-worker", "provider-worker", "publisher"}
    for role, value in old.items():
        host = value["HostConfig"]
        assert value["State"]["Running"] and value["Config"]["Labels"].get("contribos.owner") == "upeve-acceptance"
        assert value["Config"]["User"] == "501:20" and host["ReadonlyRootfs"] and not host["Privileged"]
        assert host["CapDrop"] == ["ALL"] and host["SecurityOpt"] == ["no-new-privileges:true"]
        assert not host.get("ContainerIDFile")
        assert any(m.get("Name") == state["business_volume"] for m in value["Mounts"])
        assert all(m["Type"] == "volume" or role == "sandbox-worker" and m["Destination"] == "/var/run/docker.sock" for m in value["Mounts"])
        assert not any(m.get("Name") == state["publisher_private_volume"] for m in value["Mounts"]) or role == "publisher"
    before = observation(old["serve"]["Id"])
    assert before["active"] == 0 and before["integrity"] == "ok" and not before["foreign_keys"]
    backup = Path("/tmp/harness-before-deploy-" + manifest["source_hash"][:12] + ".db")
    code = ('import sqlite3,sys;source=sqlite3.connect("file:/data/state.db?mode=ro",uri=True);'
        'target=sqlite3.connect(":memory:");source.backup(target);sys.stdout.buffer.write(target.serialize())')
    with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as target:
        subprocess.run(["docker", "exec", old["serve"]["Id"], "python", "-c", code], stdout=target, check=True)
    shutil.copy2(STATE, STATE.with_name("container-roles.before-harness-" + manifest["source_hash"][:12] + ".json"))
    # Stop every old writer before the forward migration; no old container is deleted.
    for role in ("provider-worker", "worker", "sandbox-worker", "serve", "publisher"):
        subprocess.run(["docker", "stop", "--time", "10", old[role]["Id"]], check=True, capture_output=True)
    endpoint = docker_json("context", "inspect")[0]["Endpoints"]["docker"]["Host"]
    assert endpoint.startswith("unix://")
    new = {}
    with httpx.Client(transport=httpx.HTTPTransport(uds=endpoint[7:]), base_url="http://docker", timeout=30) as engine:
        for role, value in old.items():
            config = {k: value["Config"][k] for k in ("User", "Env", "Entrypoint", "Cmd", "WorkingDir", "ExposedPorts", "Healthcheck") if k in value["Config"]}
            config["Labels"] = {**value["Config"]["Labels"], "contribos.source": "worktree-" + manifest["source_hash"]}
            config["Image"] = build["images"]["worker" if role == "sandbox-worker" else "app"]
            config["HostConfig"] = value["HostConfig"]  # All original resource/security/log limits, verbatim.
            if role != "sandbox-worker":
                config["NetworkingConfig"] = {"EndpointsConfig": {state["network"]: {"Aliases": ["publisher"] if role == "publisher" else []}}}
            name = "harness-" + ("api" if role == "serve" else role) + "-" + manifest["source_hash"][:12]
            response = engine.post("/containers/create", params={"name": name}, json=config)
            if response.status_code != 201:
                raise RuntimeError("Owned role creation failed; reconcile stopped roles, do not blindly retry")
            new[role] = {"id": response.json()["Id"], "name": name}
        state.update(roles=new, source_revision="worktree-" + manifest["source_hash"],
            source_manifest_hash=manifest["source_hash"], deployment_status="created_not_started")
        STATE.write_text(json.dumps(state, indent=2) + "\n")
        for role in ("serve", "publisher", "sandbox-worker", "provider-worker", "worker"):
            engine.post("/containers/" + new[role]["id"] + "/start").raise_for_status()
    health = {}
    with httpx.Client(timeout=10, trust_env=False) as client:
        for port in (18090, 18091, 18092, 18080):
            for _ in range(30):
                try:
                    response = client.get(f"http://127.0.0.1:{port}/health")
                    response.raise_for_status()
                    health[str(port)] = response.json()
                    break
                except httpx.HTTPError:
                    time.sleep(0.5)
            else:
                raise RuntimeError("Health not ready; reconcile owned roles")
        response = client.get(f"http://127.0.0.1:18090/api/v1/tasks/{TASK}/workbench")
        response.raise_for_status()
        view = response.json()
    assert view["execution"]["agent_runtime_v2"]["checkpoint"]["state"] == "blocked"
    after = observation(new["serve"]["id"])
    assert after["active"] == 0 and after["integrity"] == "ok" and not after["foreign_keys"]
    assert {k: v for k, v in before["tables"].items() if k != "_schema_migrations"} == {
        k: v for k, v in after["tables"].items() if k != "_schema_migrations"}
    assert after["migrations"][:len(before["migrations"])] == before["migrations"]
    prior_revisions = {r[0] for r in before["migrations"]}
    expected_migrations = [r for r in ("0042_agent_response_evidence", "0043_implementation_progress")
        if r not in prior_revisions]
    assert [r[0] for r in after["migrations"][len(before["migrations"]):]] == expected_migrations
    for role, item in new.items():
        value = docker_json("inspect", item["id"])[0]
        assert value["State"]["Running"] and value["Config"]["Env"] == old[role]["Config"]["Env"]
        for field in ("User", "Entrypoint", "Cmd", "WorkingDir"):
            assert value["Config"][field] == old[role]["Config"][field]
        assert value["HostConfig"] == old[role]["HostConfig"]
    state["deployment_status"] = "healthy"
    STATE.write_text(json.dumps(state, indent=2) + "\n")
    result = {"version": "harness-deployment-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
        "source_manifest_file": str(args.manifest), "build_report_file": str(args.build_report),
        "source_hash": manifest["source_hash"], "images": build["images"], "health": health,
        "old_roles_preserved_stopped": {k: v["Id"] for k, v in old.items()}, "new_roles": new,
        "business_volume": state["business_volume"], "old_business_table_hashes_preserved": before["tables"],
        "migration_records_appended": len(expected_migrations), "old_migrations_preserved": True, "active_jobs": 0,
        "security_resources_mounts_and_environment_unchanged": True, "Gateway_not_restarted": True,
        "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(), "model_calls": 0, "third_party_writes": 0}
    ensure_no_sensitive_data(result, context="Harness deployment report")
    with out.open("x") as target:
        target.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"status": "healthy", "source_hash": manifest["source_hash"], "health": health,
        "business_tables_preserved": len(before["tables"]) - 1, "new_migrations": len(expected_migrations), "model_calls": 0}))


if __name__ == "__main__":
    main()
