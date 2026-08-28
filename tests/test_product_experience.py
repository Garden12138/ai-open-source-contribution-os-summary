from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.api import create_app
from app.config import Settings
from app.database import Database
from app.models import (
    Opportunity,
    OpportunitySnapshot,
    Repository,
    ScanRun,
    ScoreVersion,
)
from app.product_experience import ProductExperienceService
from app.provenance import content_hash


def _seed_candidates(database: Database) -> tuple[int, int, str]:
    now = datetime(2026, 8, 28, 8, tzinfo=timezone.utc)
    scan_id = str(uuid4())
    with database.session() as session:
        scan = ScanRun(
            id=scan_id,
            status="completed",
            provenance_status="verified",
            selection_date=date(2026, 8, 28),
            queries=["offline"],
            candidate_count=2,
            eligible_count=2,
            selected_count=2,
            repository_count=2,
            started_at=now,
            completed_at=now,
        )
        session.add(scan)
        session.flush()
        ids: list[int] = []
        candidates = (
            (
                "bounty/repo",
                "Python",
                "$500 bounty: improve diagnostics",
                True,
                500.0,
                {
                    "reward_reliability": 100.0,
                    "acceptance_probability": 60.0,
                    "tech_match": 80.0,
                    "project_impact": 40.0,
                    "issue_clarity": 80.0,
                    "competition": 80.0,
                    "learning_value": 60.0,
                },
            ),
            (
                "impact/repo",
                "TypeScript",
                "Improve the public agent API",
                False,
                None,
                {
                    "reward_reliability": 0.0,
                    "acceptance_probability": 95.0,
                    "tech_match": 80.0,
                    "project_impact": 100.0,
                    "issue_clarity": 80.0,
                    "competition": 70.0,
                    "learning_value": 90.0,
                },
            ),
        )
        for index, (
            full_name,
            language,
            title,
            has_bounty,
            bounty_amount,
            components,
        ) in enumerate(candidates, start=1):
            repository = Repository(
                github_id=10_000 + index,
                full_name=full_name,
                description=f"{full_name} description",
                html_url=f"https://github.com/{full_name}",
                language=language,
                license_spdx="MIT",
                stars=1_000 * index,
                forks=20,
                open_issues=10,
                archived=False,
                disabled=False,
                default_branch="main",
                topics=[],
                pushed_at=now,
                has_contributing_guide=True,
                last_synced_at=now,
            )
            session.add(repository)
            session.flush()
            opportunity = Opportunity(
                github_issue_id=20_000 + index,
                repository_id=repository.id,
                issue_number=index,
                title=title,
                body="A complete acceptance checklist is available for contributors.",
                html_url=f"https://github.com/{full_name}/issues/{index}",
                state="open",
                labels=["bounty"] if has_bounty else ["help wanted"],
                comments_count=index,
                assignees_count=0,
                source_queries=["offline"],
                issue_created_at=now - timedelta(days=5),
                issue_updated_at=now,
                first_seen_at=now,
                last_seen_at=now,
                eligible=True,
                filter_reasons=[],
                score_total=73.4 if has_bounty else 67.9,
                score_components=components,
                risk_penalty=0,
                risk_reasons=[],
                has_bounty=has_bounty,
                bounty_amount_usd=bounty_amount,
                is_strategic=not has_bounty,
                is_tech_match=True,
            )
            session.add(opportunity)
            session.flush()
            ids.append(opportunity.id)
            snapshot = OpportunitySnapshot(
                id=str(uuid4()),
                scan_run_id=scan.id,
                opportunity_id=opportunity.id,
                schema_version="1",
                inputs_hash=content_hash({"candidate": index}),
                issue_data={
                    "title": title,
                    "body": opportunity.body,
                    "labels": opportunity.labels,
                    "state": "open",
                },
                repository_data={"full_name": full_name, "language": language},
                source_queries=["offline"],
                rule_config={},
                filter_eligible=True,
                filter_reasons=[],
                captured_at=now,
                created_at=now,
            )
            session.add(snapshot)
            session.flush()
            session.add(
                ScoreVersion(
                    id=str(uuid4()),
                    snapshot_id=snapshot.id,
                    algorithm_version="test-v1",
                    schema_version="1",
                    inputs_hash=snapshot.inputs_hash,
                    output_hash=content_hash({"score": index}),
                    score_total=opportunity.score_total,
                    score_components=components,
                    risk_penalty=0,
                    risk_reasons=[],
                    has_bounty=has_bounty,
                    bounty_amount_usd=bounty_amount,
                    is_strategic=not has_bounty,
                    is_tech_match=True,
                    created_at=now,
                )
            )
        session.commit()
    return ids[0], ids[1], scan_id


def test_goal_profiles_rerank_full_pool_without_overwriting_rule_scores(
    tmp_path,
) -> None:
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'product.db'}")
    database.create_schema()
    bounty_id, impact_id, _ = _seed_candidates(database)
    try:
        with database.session() as session:
            product = ProductExperienceService(session)
            product.create_preference(
                primary_goal="impact",
                preferred_languages=["Python", "TypeScript"],
                weekly_hours=5,
                minimum_bounty_usd=0,
                auto_scan_enabled=False,
                auto_scan_local_time="09:00",
            )
            impact_feed = product.recommendations(
                default_languages=("Python", "TypeScript")
            )
            product.create_preference(
                primary_goal="bounty",
                preferred_languages=["Python", "TypeScript"],
                weekly_hours=5,
                minimum_bounty_usd=200,
                auto_scan_enabled=False,
                auto_scan_local_time="09:00",
            )
            bounty_feed = product.recommendations(
                default_languages=("Python", "TypeScript")
            )
            assert impact_feed["items"][0]["opportunity"]["id"] == impact_id
            assert bounty_feed["items"][0]["opportunity"]["id"] == bounty_id
            assert session.get(Opportunity, bounty_id).score_total == 73.4
            assert session.get(Opportunity, impact_id).score_total == 67.9
    finally:
        database.close()


def test_product_api_shortlist_compare_reminders_and_notifications(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'api.db'}"
    app = create_app(Settings(database_url=database_url))
    app.state.database.create_schema()
    bounty_id, impact_id, scan_id = _seed_candidates(app.state.database)
    with app.state.database.session() as session:
        ProductExperienceService(session).create_scan_notifications(scan_id)

    with TestClient(app) as client:
        meta = client.get("/api/v1/meta").json()
        mutation_headers = {
            "Origin": "http://testserver",
            "X-CSRF-Token": meta["csrf_token"],
        }
        preference = client.post(
            "/api/v1/preferences/current",
            headers=mutation_headers,
            json={
                "primary_goal": "balanced",
                "preferred_languages": ["Python", "TypeScript"],
                "weekly_hours": 5,
                "minimum_bounty_usd": 0,
                "auto_scan_enabled": False,
                "auto_scan_local_time": "09:00",
            },
        )
        assert preference.status_code == 200
        for opportunity_id in (bounty_id, impact_id):
            saved = client.post(
                f"/api/v1/opportunities/{opportunity_id}/dispositions",
                headers=mutation_headers,
                json={"state": "shortlisted"},
            )
            assert saved.status_code == 200

        shortlist = client.get("/api/v1/shortlist")
        comparison = client.get(
            "/api/v1/opportunities/compare",
            params=[
                ("opportunity_id", bounty_id),
                ("opportunity_id", impact_id),
            ],
        )
        notifications = client.get("/api/v1/notifications")
        assert shortlist.status_code == 200
        assert len(shortlist.json()) == 2
        assert comparison.status_code == 200
        assert len(comparison.json()) == 2
        assert notifications.status_code == 200
        assert len(notifications.json()) == 1
        notification_id = notifications.json()[0]["id"]
        read = client.post(
            f"/api/v1/notifications/{notification_id}/read",
            headers=mutation_headers,
            json={},
        )
        canary = "github_pat_PRODUCT_EXPERIENCE_CANARY_123456"
        rejected_preference = client.post(
            "/api/v1/preferences/current",
            headers=mutation_headers,
            json={
                "preferred_languages": [canary],
            },
        )
        assert read.status_code == 200
        assert rejected_preference.status_code == 422
        assert canary not in rejected_preference.text
        assert client.get(
            "/api/v1/notifications", params={"unread_only": True}
        ).json() == []


def test_preference_history_and_due_reminders_are_immutable_and_idempotent(
    tmp_path,
) -> None:
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'reminders.db'}")
    database.create_schema()
    bounty_id, _, _ = _seed_candidates(database)
    now = datetime(2026, 8, 28, 10, tzinfo=timezone.utc)
    try:
        with database.session() as session:
            product = ProductExperienceService(session)
            first = product.create_preference(
                primary_goal="balanced",
                preferred_languages=["Python"],
                weekly_hours=5,
                minimum_bounty_usd=0,
                auto_scan_enabled=False,
                auto_scan_local_time="09:00",
            )
            second = product.create_preference(
                primary_goal="impact",
                preferred_languages=["Python"],
                weekly_hours=5,
                minimum_bounty_usd=0,
                auto_scan_enabled=False,
                auto_scan_local_time="09:00",
            )
            assert first.version == 1
            assert second.version == 2
            disposition = product.set_disposition(
                bounty_id,
                state="shortlisted",
                reason_code=None,
                reminder_at=now - timedelta(minutes=1),
            )
            assert product.sync_due_reminders(now=now) == 1
            assert product.sync_due_reminders(now=now + timedelta(hours=1)) == 0
            reminders = product.notifications()
            assert [item["kind"] for item in reminders] == ["reminder_due"]
            assert reminders[0]["opportunity_id"] == bounty_id
            read = product.mark_notification_read(str(reminders[0]["id"]))

        with pytest.raises(IntegrityError, match="immutable"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE user_preference_versions "
                        "SET weekly_hours = 10 WHERE id = :id"
                    ),
                    {"id": second.id},
                )
        with pytest.raises(IntegrityError, match="immutable"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE opportunity_disposition_versions "
                        "SET state = 'neutral' WHERE id = :id"
                    ),
                    {"id": disposition.id},
                )
        with pytest.raises(IntegrityError, match="immutable"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "DELETE FROM notification_reads "
                        "WHERE notification_id = :id"
                    ),
                    {"id": read.notification_id},
                )
    finally:
        database.close()


def test_shortlisted_score_change_creates_one_scan_update_notification(
    tmp_path,
) -> None:
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'changes.db'}")
    database.create_schema()
    bounty_id, _, _ = _seed_candidates(database)
    later = datetime(2026, 8, 29, 8, tzinfo=timezone.utc)
    try:
        with database.session() as session:
            product = ProductExperienceService(session)
            product.set_disposition(
                bounty_id,
                state="shortlisted",
                reason_code=None,
                reminder_at=None,
            )
            previous_snapshot = session.scalar(
                select(OpportunitySnapshot).where(
                    OpportunitySnapshot.opportunity_id == bounty_id
                )
            )
            assert previous_snapshot is not None
            previous_score = session.scalar(
                select(ScoreVersion).where(
                    ScoreVersion.snapshot_id == previous_snapshot.id
                )
            )
            assert previous_score is not None
            scan = ScanRun(
                id=str(uuid4()),
                status="completed",
                provenance_status="verified",
                selection_date=later.date(),
                queries=["offline"],
                candidate_count=1,
                eligible_count=0,
                selected_count=0,
                repository_count=1,
                started_at=later,
                completed_at=later,
            )
            session.add(scan)
            session.flush()
            snapshot = OpportunitySnapshot(
                id=str(uuid4()),
                scan_run_id=scan.id,
                opportunity_id=bounty_id,
                schema_version="1",
                inputs_hash=content_hash({"candidate": "rescored"}),
                issue_data=dict(previous_snapshot.issue_data),
                repository_data=dict(previous_snapshot.repository_data),
                source_queries=list(previous_snapshot.source_queries),
                rule_config=dict(previous_snapshot.rule_config),
                filter_eligible=False,
                filter_reasons=["became_ineligible"],
                captured_at=later,
                created_at=later,
            )
            session.add(snapshot)
            session.flush()
            session.add(
                ScoreVersion(
                    id=str(uuid4()),
                    snapshot_id=snapshot.id,
                    algorithm_version="test-v2",
                    schema_version="1",
                    inputs_hash=snapshot.inputs_hash,
                    output_hash=content_hash({"score": "changed"}),
                    score_total=previous_score.score_total + 1,
                    score_components=dict(previous_score.score_components),
                    risk_penalty=previous_score.risk_penalty,
                    risk_reasons=list(previous_score.risk_reasons),
                    has_bounty=previous_score.has_bounty,
                    bounty_amount_usd=previous_score.bounty_amount_usd,
                    is_strategic=previous_score.is_strategic,
                    is_tech_match=previous_score.is_tech_match,
                    created_at=later,
                )
            )
            session.commit()

            assert product.create_scan_notifications(scan.id) == 1
            assert product.create_scan_notifications(scan.id) == 0
            notifications = product.notifications()
            assert len(notifications) == 1
            assert notifications[0]["kind"] == "shortlist_updated"
            changes = product.latest_scan_changes()
            assert changes["shortlist_updates"] == 1
    finally:
        database.close()


def test_notification_copy_redacts_configured_secret(tmp_path) -> None:
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'redacted.db'}")
    database.create_schema()
    bounty_id, _, scan_id = _seed_candidates(database)
    secret = "local-notification-secret-value"
    try:
        with database.session() as session:
            opportunity = session.get(Opportunity, bounty_id)
            assert opportunity is not None
            opportunity.title = secret
            session.commit()
            product = ProductExperienceService(session, secrets=(secret,))
            assert product.create_scan_notifications(scan_id) == 1
            notifications = product.notifications()
            assert secret not in str(notifications)
            assert notifications[0]["message"] == "[REDACTED]"
    finally:
        database.close()
