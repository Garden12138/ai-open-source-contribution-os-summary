"""GET-only check of the original Issue and main, using the existing read proxy."""
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
    previous = json.loads((ROOT.parent / "plan14-preparation/upstream-check.json").read_text())
    settings = Settings(github_read_proxy_url="http://127.0.0.1:18092",
        github_read_proxy_key=bytes.fromhex(Path("/tmp/upeve-isolated-20261029-v2/github-read-proxy-key").read_text().strip()),
        github_max_retries=0, request_timeout_seconds=30)
    async with GitHubClient(settings) as github:
        items = await github.search_issues('repo:patoles/agent-flow is:issue "tool" "duplicate"', 100)
        issue, = [i for i in items if i.get("number") == 107 and i.get("html_url") == previous["issue_url"]]
        branch, head = await github.resolve_repository_head("patoles/agent-flow", "main")
    value = {"version": "output-capacity-plan15-upstream-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
        "transport": "existing_allowlisted_read_only_search_and_head_proxy", "issue_url": issue["html_url"],
        "state": issue["state"], "title": issue["title"], "updated_at": issue["updated_at"],
        "issue_body_hash": content_hash(issue["body"]),
        "issue_body_hash_encoding": "canonical_JSON_string_SHA256_matches_frozen_evidence",
        "issue_body_raw_utf8_sha256": hashlib.sha256(issue["body"].encode()).hexdigest(),
        "branch": branch, "HEAD": head, "model_calls": 0, "third_party_writes": 0}
    differences = [key for key in ("issue_url", "state", "title", "updated_at", "issue_body_hash", "branch", "HEAD")
        if value[key] != previous[key]]
    value.update(changed_fields=differences, issue_body_matches=value["issue_body_hash"] == previous["issue_body_hash"],
        baseline_matches=value["HEAD"] == previous["HEAD"])
    ensure_no_sensitive_data(value, context="Plan15 upstream check")
    value["record_hash"] = content_hash(value)
    with (ROOT / "upstream-check.json").open("x") as target:
        target.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(value, ensure_ascii=False))
    assert not differences, "Frozen upstream changed: " + ", ".join(differences)


if __name__ == "__main__":
    asyncio.run(main())
