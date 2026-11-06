"""Fresh GET-only upstream check using the existing allowlisted read proxy."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from app.config import Settings
from app.github import GitHubClient
from app.provenance import content_hash
from app.security import ensure_no_sensitive_data

ROOT = Path(__file__).resolve().parent


async def main() -> None:
    previous = json.loads((ROOT.parent / "plan13-preparation/upstream-check.json").read_text())
    settings = Settings(github_read_proxy_url="http://127.0.0.1:18092",
        github_read_proxy_key=bytes.fromhex(Path("/tmp/upeve-isolated-20261029-v2/github-read-proxy-key").read_text().strip()),
        github_max_retries=0, request_timeout_seconds=30)
    async with GitHubClient(settings) as github:
        items = await github.search_issues('repo:patoles/agent-flow is:issue "tool" "duplicate"', 100)
        issue, = [i for i in items if i.get("number") == 107 and i.get("html_url") == previous["issue_url"]]
        branch, head = await github.resolve_repository_head("patoles/agent-flow", "main")
    value = {"version": "approved-missing-plan14-upstream-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
        "transport": "existing_allowlisted_read_only_search_and_head_proxy", "issue_url": issue["html_url"],
        "state": issue["state"], "title": issue["title"], "updated_at": issue["updated_at"],
        "issue_body_hash": content_hash(issue["body"]), "issue_body_hash_encoding": "canonical_JSON_string_SHA256_matches_frozen_evidence",
        "issue_body_raw_utf8_sha256": hashlib.sha256(issue["body"].encode()).hexdigest(), "branch": branch, "HEAD": head,
        "model_calls": 0, "third_party_writes": 0}
    for key in ("issue_url", "state", "title", "updated_at", "issue_body_hash", "branch", "HEAD"):
        assert value[key] == previous[key], f"Frozen upstream changed: {key}"
    value.update(issue_body_matches=True, baseline_matches=True)
    ensure_no_sensitive_data(value, context="Plan14 upstream check")
    value["record_hash"] = content_hash(value)
    with (ROOT / "upstream-before-execution.json").open("x") as target:
        target.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(value, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
