from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import create_app
from app.change_scope import assess_change_scope
from app.config import Settings
from app.database import Database
from app.models import Opportunity, ScoreVersion
from app.service import DiscoveryService
from tests.test_analysis_evidence import _CandidateGitHub, NOW


@pytest.mark.parametrize("title,body,labels,classification", [
    ("Fix typo in README", "", [], "likely_small"),
    ("Fix typo in migration guide", "", [], "likely_small"),
    ("Fix typo and redesign homepage", "", [], "excluded"),
    ("Migrate acceptance tests to Playwright", "Only one file.", ["good first issue"], "excluded"),
    ("Improve startup", "We must refactor all modules.", ["size: s"], "excluded"),
    ("Fix Puppeteer test", "A single file.", ["good first issue"], "excluded"),
    ("Small fix", "Requires a running Redis server.", ["size: xs"], "excluded"),
    ("Fix empty input handling", "Add a regression test.", [], "likely_small"),
    ("Fix localized bug", "Limit changes to a single function.", [], "likely_small"),
    ("Improve public API", "Acceptance criteria need discussion.", ["good first issue"], "unknown"),
    ("Improve behavior", "Only modify the implementation.", ["help wanted"], "unknown"),
    ("Fix typo", "x" * 60001, [], "excluded"),
    ("Fix typo", "", ["size: xl"], "excluded"),
    ("Fix streamed fields", "", ["size:S"], "likely_small"),
    ("Fix streamed fields", "", [" Size / XS "], "likely_small"),
    ("Fix typo", "", ["size:XL"], "excluded"),
    ("Fix typo", "", ["size / L"], "excluded"),
    ("Add Changelog and Documentation links to the PyPI project page", "Add entries to [project.urls] in pyproject.toml.", [], "likely_small"),
    ("Add current judge-flow screenshots to README", "Update README.md. No product logic changes.", [], "likely_small"),
    ("Add links to documentation", "Edit CONTRIBUTING.md.", [], "likely_small"),
    ("Improve all documentation", "Rewrite our guides.", ["documentation"], "unknown"),
    ("Add links to documentation", "Refactor all modules; edit README.md.", [], "excluded"),
    ("Add screenshots to README", "Requires a running database. README.md", [], "excluded"),
    ("Add links to PyPI project page", "Create a new publishing platform.", [], "unknown"),
])
def test_small_change_hints_are_conservative(title, body, labels, classification):
    result = assess_change_scope(title=title, body=body, labels=labels)
    assert result["classification"] == classification
    assert result["rule_version"] == "change-scope-v2"


def test_existing_effort_can_exclude_large_work_without_turning_unknown_into_small():
    assert assess_change_scope(title="Fix typo", body="", labels=[],
        effort={"size":"l", "hours_max":20})["classification"] == "excluded"
    assert assess_change_scope(title="Improve API", body="", labels=[],
        effort={"size":"unknown", "hours_max":None})["classification"] == "unknown"
    assert assess_change_scope(title="Fix API behavior", body="", labels=[],
        effort={"size":"s", "hours_max":3})["reason_codes"] == ["small_effort_estimate"]


class ScopeGitHub(_CandidateGitHub):
    async def search_issues(self, query, limit):
        issues = await super().search_issues(query, limit)
        for i, issue in enumerate(issues):
            issue["title"] = ["Migrate all tests to Playwright", "Improve public API", "Fix typo in README", "Fix wrong parameter default"][i]
            issue["labels"] = [{"name":"good first issue"}]
            if i >= 2:
                issue["comments"] = 25  # Small work is intentionally outside the first unfiltered page.
        return issues


def scoped_database(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'scope.db'}", artifact_root=str(tmp_path / 'artifacts'),
        github_queries=("scope-fixture",), analysis_provider="fake", local_access_token="scope-local-test-token")
    db = Database(settings.database_url)
    db.create_schema()
    with db.session() as session:
        asyncio.run(DiscoveryService(session, ScopeGitHub(4), settings).scan(now=NOW, top_n=4))
    return db, settings


def test_filter_uses_full_snapshot_pool_before_pagination_and_keeps_scores_immutable(tmp_path):
    db, settings = scoped_database(tmp_path)
    with db.session() as session:
        scores = [(s.id, s.output_hash, s.score_total) for s in session.scalars(select(ScoreVersion))]
        # Changing the live record must not reclassify the frozen migration Issue.
        migration = session.scalar(select(Opportunity).where(Opportunity.issue_number == 1))
        migration.title = "Fix typo in README"
        session.commit()
    with TestClient(create_app(settings)) as client:
        url = "/api/v1/recommendations"
        all_items = client.get(url + "?limit=1").json()
        assert all_items["total"] == 4
        assert all_items["small_change_total"] == 2
        assert all_items["items"][0]["change_scope"]["classification"] != "likely_small"
        first = client.get(url + "?small_change_only=true&limit=1").json()
        second = client.get(url + "?small_change_only=true&limit=1&offset=1").json()
        assert first["total"] == 2 and first["small_change_only"]
        assert first["pending_analysis_total"] == 2
        assert first["items"][0]["snapshot_id"] != second["items"][0]["snapshot_id"]
        assert all(x["change_scope"]["classification"] == "likely_small" for x in first["items"] + second["items"])
        assert client.get(url + "?small_change_only=true&analysis_filter=recommended").json()["total"] == 0
        assert client.get(url + "?small_change_only=nonsense").status_code == 422
    with db.session() as session:
        assert [(s.id, s.output_hash, s.score_total) for s in session.scalars(select(ScoreVersion))] == scores


def test_filtered_batch_analysis_only_queues_small_changes_and_remains_authorized(tmp_path):
    _, settings = scoped_database(tmp_path)
    with TestClient(create_app(settings)) as client:
        url = "/api/v1/recommendations"
        expected = {x["snapshot_id"] for x in client.get(url + "?small_change_only=true").json()["items"]}
        payload = {"limit":5, "small_change_only":True}
        assert client.post(url + "/analyses", json=payload).status_code == 401
        meta = client.get("/api/v1/meta").json()
        headers = {"Origin":"http://testserver", "X-CSRF-Token":meta["csrf_token"], "Idempotency-Key":"small-change-analysis", "X-ContribOS-Token":"scope-local-test-token"}
        response = client.post(url + "/analyses", json=payload, headers=headers)
        assert response.status_code == 202, response.text
        assert response.json()["requested"] == 2
        assert {x["snapshot_id"] for x in response.json()["jobs"]} == expected


def test_empty_candidate_pool_has_consistent_filter_metadata(tmp_path):
    with TestClient(create_app(Settings(database_url=f"sqlite:///{tmp_path / 'empty.db'}"))) as client:
        data = client.get("/api/v1/recommendations?small_change_only=true").json()
        assert data["items"] == []
        assert data["total"] == data["small_change_total"] == 0
        assert data["small_change_only"]


def test_rescan_classifies_new_snapshots_instead_of_reusing_old_matches(tmp_path):
    class RescanGitHub(_CandidateGitHub):
        async def search_issues(self, query, limit):
            issues = await super().search_issues(query, limit)
            for index, issue in enumerate(issues):
                issue["id"] += 100
                issue["number"] += 100
                issue["html_url"] = f"https://github.com/fixture/evidence/issues/{issue['number']}"
                issue["title"] = ["Streamed fields are replaced", "Add Changelog links to PyPI project page", "Migrate all tests", "Improve public API"][index]
                issue["labels"] = [{"name":"size:S"}] if index == 0 else [{"name":"good first issue"}]
                if index == 1:
                    issue["body"] = "Add Changelog to [project.urls] in pyproject.toml and verify the built wheel metadata."
            return issues

    db, settings = scoped_database(tmp_path)
    with TestClient(create_app(settings)) as client:
        url = "/api/v1/recommendations?small_change_only=true"
        previous = client.get(url).json()
        with db.session() as session:
            asyncio.run(DiscoveryService(session, RescanGitHub(4), settings).scan(now=NOW + timedelta(days=1), top_n=4))
        current = client.get(url).json()
        assert current["scan_run_id"] != previous["scan_run_id"]
        assert current["total"] == current["small_change_total"] == 2
        assert all(item["opportunity"]["issue_number"] > 100 for item in current["items"])
        assert {code for item in current["items"] for code in item["change_scope"]["reason_codes"]} >= {"small_size_label", "package_metadata_links"}
        assert all(item["change_scope"]["rule_version"] == "change-scope-v2" for item in current["items"])


def test_default_discovery_targets_text_fixes_and_preserves_custom_queries(monkeypatch):
    monkeypatch.delenv("GITHUB_QUERIES", raising=False)
    queries = Settings.from_env().github_queries
    assert all(any("typo in:title" in q and f"language:{language}" in q for q in queries) for language in ["Python", "TypeScript"])
    monkeypatch.setenv("GITHUB_QUERIES", "repo:acme/custom is:issue is:open")
    assert Settings.from_env().github_queries == ("repo:acme/custom is:issue is:open",)
