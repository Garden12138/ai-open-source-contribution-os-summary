from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.database import Database
from app.models import (
    DailyPick,
    Opportunity,
    OpportunitySnapshot,
    Repository,
    ScanRun,
    ScoreVersion,
)
from app.service import DiscoveryService


NOW = datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc)


def issue_payload(
    issue_id: int,
    repository: str,
    number: int,
    title: str,
    *,
    body: str | None = None,
) -> dict[str, Any]:
    return {
        "id": issue_id,
        "number": number,
        "title": title,
        "body": body
        or (
            "Steps to reproduce and expected behavior are described in enough detail "
            "for a contributor to implement and verify the change."
        ),
        "html_url": f"https://github.com/{repository}/issues/{number}",
        "repository_url": f"https://api.github.com/repos/{repository}",
        "state": "open",
        "labels": [{"name": "help wanted"}],
        "comments": 0,
        "assignees": [],
        "author_association": "MEMBER",
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": "2026-07-16T00:00:00Z",
    }


def repository_bundle(full_name: str, github_id: int) -> dict[str, Any]:
    return {
        "repository": {
            "id": github_id,
            "full_name": full_name,
            "description": f"AI tooling in {full_name}",
            "html_url": f"https://github.com/{full_name}",
            "language": "Python",
            "license": {"spdx_id": "MIT"},
            "stargazers_count": 1_000,
            "forks_count": 100,
            "open_issues_count": 20,
            "archived": False,
            "disabled": False,
            "default_branch": "main",
            "topics": ["ai", "mcp"],
            "pushed_at": "2026-07-15T00:00:00Z",
        },
        "community": {
            "health_percentage": 90,
            "files": {"contributing": {"url": "https://example.test/guide"}},
        },
    }


class FakeGitHub:
    rate_limit_remaining = 4_999
    rate_limit_reset_at = NOW + timedelta(hours=1)

    def __init__(self) -> None:
        duplicate = issue_payload(
            101,
            "acme/alpha",
            1,
            "$250 bounty: improve the AI agent",
        )
        self.search_results = {
            "query-one": [
                duplicate,
                issue_payload(102, "acme/beta", 2, "Add MCP command support"),
            ],
            "query-two": [
                duplicate,
                issue_payload(103, "acme/alpha", 3, "Clarify contributor diagnostics"),
            ],
        }
        self.bundles = {
            "acme/alpha": repository_bundle("acme/alpha", 201),
            "acme/beta": repository_bundle("acme/beta", 202),
        }
        self.search_calls: list[tuple[str, int]] = []
        self.repository_calls: list[str] = []

    async def search_issues(self, query: str, limit: int) -> list[dict[str, Any]]:
        self.search_calls.append((query, limit))
        return [dict(item) for item in self.search_results[query]]

    async def get_repository_bundle(self, full_name: str) -> dict[str, Any]:
        self.repository_calls.append(full_name)
        return self.bundles[full_name]


class RecoveringGitHub(FakeGitHub):
    def __init__(self) -> None:
        super().__init__()
        self.search_results = {"query-one": [self.search_results["query-one"][0]]}
        self.failed_once = False

    async def get_repository_bundle(self, full_name: str) -> dict[str, Any]:
        self.repository_calls.append(full_name)
        if not self.failed_once:
            self.failed_once = True
            raise RuntimeError("temporary metadata failure")
        return self.bundles[full_name]


@pytest.fixture
def database(tmp_path: Any) -> Database:
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'service.db'}")
    database.create_schema()
    try:
        yield database
    finally:
        database.close()


def test_scan_deduplicates_caches_and_persists_daily_picks(database: Database) -> None:
    github = FakeGitHub()
    settings = Settings(
        database_url=str(database.engine.url),
        github_queries=("query-one", "query-two"),
        candidates_per_query=10,
        daily_pick_count=2,
        repository_cache_hours=24,
    )

    with database.session() as session:
        first_run = asyncio.run(
            DiscoveryService(session, github, settings).scan(now=NOW, top_n=2)
        )

    assert first_run.status == "completed"
    assert first_run.candidate_count == 3
    assert first_run.repository_count == 2
    assert first_run.eligible_count == 3
    assert first_run.selected_count == 2
    assert first_run.rate_limit_remaining == 4_999
    assert github.repository_calls == ["acme/alpha", "acme/beta"]

    with database.session() as session:
        duplicate = session.scalar(
            select(Opportunity).where(Opportunity.github_issue_id == 101)
        )
        assert duplicate is not None
        assert duplicate.source_queries == ["query-one", "query-two"]
        assert session.scalar(select(func.count()).select_from(Opportunity)) == 3
        assert (
            session.scalar(select(func.count()).select_from(OpportunitySnapshot)) == 3
        )
        assert session.scalar(select(func.count()).select_from(ScoreVersion)) == 3
        assert session.scalar(select(func.count()).select_from(Repository)) == 2
        picks = list(session.scalars(select(DailyPick).order_by(DailyPick.rank)))
        assert [pick.rank for pick in picks] == [1, 2]
        assert {pick.selection_reason for pick in picks} == {"bounty", "strategic"}
        assert all(pick.provenance_status == "verified" for pick in picks)
        assert all(pick.scan_run_id == first_run.id for pick in picks)
        assert all(pick.snapshot_id and pick.score_version_id for pick in picks)
        assert all(
            pick.snapshot is not None
            and pick.snapshot.opportunity_id == pick.opportunity_id
            and pick.snapshot.scan_run_id == pick.scan_run_id
            and pick.score_version is not None
            and pick.score_version.snapshot_id == pick.snapshot_id
            for pick in picks
        )

    github.search_results["query-one"][1]["title"] = "Updated MCP command support"
    with database.session() as session:
        second_run = asyncio.run(
            DiscoveryService(session, github, settings).scan(
                now=NOW + timedelta(hours=1), top_n=2
            )
        )

    assert second_run.status == "completed"
    assert github.repository_calls == ["acme/alpha", "acme/beta"]
    assert github.search_calls == [
        ("query-one", 10),
        ("query-two", 10),
        ("query-one", 10),
        ("query-two", 10),
    ]

    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(ScanRun)) == 2
        assert session.scalar(select(func.count()).select_from(Opportunity)) == 3
        assert (
            session.scalar(select(func.count()).select_from(OpportunitySnapshot)) == 6
        )
        assert session.scalar(select(func.count()).select_from(ScoreVersion)) == 6
        assert session.scalar(select(func.count()).select_from(DailyPick)) == 2
        picks = list(session.scalars(select(DailyPick).order_by(DailyPick.rank)))
        assert all(pick.scan_run_id == second_run.id for pick in picks)
        updated = session.scalar(
            select(Opportunity).where(Opportunity.github_issue_id == 102)
        )
        assert updated is not None
        assert updated.title == "Updated MCP command support"
        persisted_last_seen = updated.last_seen_at
        if persisted_last_seen.tzinfo is None:  # SQLite stores timezone separately.
            persisted_last_seen = persisted_last_seen.replace(tzinfo=timezone.utc)
        assert persisted_last_seen == NOW + timedelta(hours=1)


def test_scan_rejects_an_out_of_range_top_n(database: Database) -> None:
    github = FakeGitHub()
    settings = Settings(database_url=str(database.engine.url))

    with (
        database.session() as session,
        pytest.raises(ValueError, match="top_n must be between 1 and 50"),
    ):
        asyncio.run(DiscoveryService(session, github, settings).scan(top_n=0))

    assert github.search_calls == []


def test_scan_retries_repository_metadata_failures(database: Database) -> None:
    github = RecoveringGitHub()
    settings = Settings(
        database_url=str(database.engine.url),
        github_queries=("query-one",),
        repository_cache_hours=24,
    )

    with database.session() as session:
        first_run = asyncio.run(
            DiscoveryService(session, github, settings).scan(now=NOW)
        )
    assert first_run.eligible_count == 0

    with database.session() as session:
        second_run = asyncio.run(
            DiscoveryService(session, github, settings).scan(
                now=NOW + timedelta(hours=1)
            )
        )
    assert second_run.eligible_count == 1
    assert github.repository_calls == ["acme/alpha", "acme/alpha"]


def test_snapshot_and_score_versions_are_database_immutable(
    database: Database,
) -> None:
    github = FakeGitHub()
    settings = Settings(
        database_url=str(database.engine.url),
        github_queries=("query-one",),
    )

    with database.session() as session:
        asyncio.run(DiscoveryService(session, github, settings).scan(now=NOW))

    with database.session() as session:
        snapshot = session.scalar(select(OpportunitySnapshot).limit(1))
        assert snapshot is not None
        snapshot.issue_data = {"title": "tampered"}
        with pytest.raises(IntegrityError, match="immutable"):
            session.commit()
        session.rollback()

        snapshot = session.scalar(select(OpportunitySnapshot).limit(1))
        assert snapshot is not None
        with pytest.raises(IntegrityError, match="immutable"):
            session.delete(snapshot)
            session.commit()
        session.rollback()

        score = session.scalar(select(ScoreVersion).limit(1))
        assert score is not None
        score.score_total = 0
        with pytest.raises(IntegrityError, match="immutable"):
            session.commit()
        session.rollback()

        with pytest.raises(IntegrityError, match="immutable"):
            session.execute(delete(ScoreVersion))
        session.rollback()


def test_daily_pick_rejects_mismatched_verified_provenance(
    database: Database,
) -> None:
    github = FakeGitHub()
    settings = Settings(
        database_url=str(database.engine.url),
        github_queries=("query-one",),
    )

    with database.session() as session:
        asyncio.run(DiscoveryService(session, github, settings).scan(now=NOW))

    with database.session() as session:
        snapshots = list(
            session.scalars(
                select(OpportunitySnapshot).order_by(
                    OpportunitySnapshot.opportunity_id
                )
            )
        )
        assert len(snapshots) == 2
        score = session.scalar(
            select(ScoreVersion).where(
                ScoreVersion.snapshot_id == snapshots[0].id
            )
        )
        assert score is not None
        session.add(
            DailyPick(
                selection_date=(NOW + timedelta(days=1)).date(),
                rank=1,
                opportunity_id=snapshots[1].opportunity_id,
                scan_run_id=snapshots[0].scan_run_id,
                snapshot_id=snapshots[0].id,
                score_version_id=score.id,
                provenance_status="verified",
                selection_reason="invalid-test",
                score_snapshot=score.score_total,
            )
        )

        with pytest.raises(IntegrityError, match="invalid daily pick provenance"):
            session.commit()
