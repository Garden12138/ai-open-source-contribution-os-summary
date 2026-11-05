"""Freeze fresh public GET observations before any real Harness execution."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import httpx

from app.provenance import content_hash
from app.security import ensure_no_sensitive_data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Observation already exists; choose a new output path")
    repo = "https://api.github.com/repos/patoles/agent-flow"
    with httpx.Client(timeout=20, trust_env=False, follow_redirects=False,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "ContribOS-Harness-read-only"}) as client:
        response = client.get(repo + "/issues/107")
        response.raise_for_status()
        issue = response.json()
        head = client.get(repo + "/commits/main")
        head.raise_for_status()
        base = client.get(repo + "/commits/3489e17c48985b74fdb42cde122376b1eb12d8a7")
        base.raise_for_status()
    value = {"version": "harness-upstream-observation-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
        "repository": "patoles/agent-flow", "issue": 107, "issue_url": issue["html_url"],
        "state": issue["state"], "updated_at": issue["updated_at"], "issue_title": issue["title"],
        "issue_body_hash": content_hash(issue.get("body")), "HEAD": head.json()["sha"],
        "baseline_exists": base.json()["sha"], "HEAD_matches_Plan11": head.json()["sha"] == base.json()["sha"],
        "HTTP_Date": response.headers.get("date"), "ETag": response.headers.get("etag"),
        "model_calls": 0, "third_party_writes": 0}
    ensure_no_sensitive_data(value, context="read-only upstream observation")
    value["record_hash"] = content_hash(value)
    with args.output.open("x") as out:
        out.write(json.dumps(value, indent=2) + "\n")
    print(json.dumps(value, indent=2))


if __name__ == "__main__":
    main()
