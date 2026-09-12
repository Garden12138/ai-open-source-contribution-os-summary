"""One user-approved planning retry via the API, run inside the API container."""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request

from app.config import Settings

TASK = "15ea2bdc-8aea-4236-9c26-05a54f16e604"
FAILED_JOB = "06f2a4d6-6c7d-449e-beee-2e5079aae0b0"
KEY = "diagnostic-retry-06f2a4d6-planning-v2"
BASE = "http://127.0.0.1:8000/api/v1/tasks/" + TASK + "/workbench"


def main() -> None:
    with sqlite3.connect("file:/data/contribos.db?mode=ro", uri=True) as database:
        previous = database.execute(
            "SELECT id,state FROM jobs WHERE idempotency_key=?", ("plan:" + KEY,)
        ).fetchone()
        if previous:
            print(json.dumps({"job_id": previous[0], "state": previous[1], "reused": True}))
            return
        binding = database.execute(
            "SELECT profile_id FROM job_model_bindings WHERE job_id=?", (FAILED_JOB,)
        ).fetchone()
    with urllib.request.urlopen(BASE, timeout=15) as response:
        state = json.load(response)
    if state["state"] != "planning" or state["busy"] or state["jobs"][-1]["id"] != FAILED_JOB:
        raise SystemExit("Task changed or busy; retry was not submitted")
    if not binding or state["model_profiles"]["planning"] != binding[0]:
        raise SystemExit("Frozen planning profile changed; retry was not submitted")
    plan = state["plans"][-1] if state["plans"] else None
    payload = {"text": "修改方案", "parent_id": plan["id"] if plan else None,
        "expected_hash": plan["record_hash"] if plan else None, "model_profile_id": binding[0]}
    headers = {"Content-Type": "application/json", "Idempotency-Key": KEY}
    token = Settings.from_env().local_access_token
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(BASE + "/messages", method="POST",
        headers=headers, data=json.dumps(payload).encode())
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            job = json.load(response)
    except urllib.error.HTTPError as error:
        raise SystemExit(f"Retry rejected (HTTP {error.code}); response body suppressed") from None
    print(json.dumps({"job_id": job["id"], "state": job["state"], "reused": False}))


if __name__ == "__main__":
    main()
