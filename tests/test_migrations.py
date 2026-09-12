from __future__ import annotations

import sqlite3
import shutil
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from app.database import Database
from app.jobs import JobService
from app.migrations import MigrationError, MigrationRunner
from app.migrations.versions import MIGRATIONS
from app.models import Base


FIXTURES = Path(__file__).parent / "fixtures"
BASELINE_REVISION = "0001_phase1_baseline"
LATEST_REVISION = "0032_minimax_reviews"


def _load_phase1_fixture(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            (FIXTURES / "phase1_schema.sql").read_text(encoding="utf-8")
        )
        connection.commit()
    finally:
        connection.close()


def test_empty_database_is_migrated_and_repeatable(tmp_path: Path) -> None:
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'empty.db'}")
    try:
        first = database.create_schema()
        second = database.create_schema()

        assert first.previous_revision is None
        assert first.current_revision == LATEST_REVISION
        assert first.applied == (
            BASELINE_REVISION,
            "0002_provenance",
            "0003_jobs",
            "0004_artifacts_audit",
            "0005_scan_selection_date",
            "0006_provider_invocations",
            "0007_analysis_versions",
            "0008_final_score_versions",
            "0009_contribution_tasks",
            "0010_contribution_task_states",
            "0011_plan_versions",
            "0012_plan_revision_links",
            "0013_plan_locks",
            "0014_plan_approvals",
            "0015_plan_conversations",
            "0016_execution_attempts",
            "0017_execution_stage_runs",
            "0018_dependency_verify_inputs",
            "0019_execution_artifact_manifests",
            "0020_execution_workspace_disposals",
            "0021_review_runs",
            "0022_publish_intents",
            "0023_pull_request_events",
            "0024_task_side_states",
            "0025_product_experience",
            "0026_review_artifact_bindings",
            "0027_nvidia_agent_workflows",
            "0028_nvidia_review_runs",
            "0029_workbench",
            "0030_model_settings",
            "0031_task_visibility",
            LATEST_REVISION,
        )
        assert first.stamped == ()
        assert second.previous_revision == LATEST_REVISION
        assert second.current_revision == LATEST_REVISION
        assert second.applied == ()
        assert second.stamped == ()
        assert database.current_revision() == LATEST_REVISION

        table_names = set(inspect(database.engine).get_table_names())
        assert table_names == {
            "_schema_migrations",
            "analysis_versions",
            "agent_invocations",
            "artifacts",
            "audit_events",
            "contribution_tasks",
            "contribution_task_state_versions",
            "coding_sessions",
            "coding_turns",
            "daily_picks",
            "execution_attempts",
            "execution_artifact_entries",
            "execution_artifact_manifests",
            "execution_stage_runs",
            "execution_stage_versions",
            "execution_workspace_disposal_versions",
            "execution_workspace_disposals",
            "final_score_versions",
            "jobs",
            "job_artifacts",
            "in_app_notifications",
            "notification_reads",
            "opportunities",
            "opportunity_disposition_versions",
            "opportunity_snapshots",
            "plan_versions",
            "plan_locks",
            "plan_approvals",
            "plan_conversation_entries",
            "publish_confirmations",
            "publish_intents",
            "provider_invocations",
            "pull_request_events",
            "change_set_proposals",
            "draft_pull_requests",
            "review_runs",
            "repositories",
            "scan_runs",
            "score_versions",
            "task_lifecycle_marks",
            "user_preference_versions",
            "workbench_events",
            "model_config_versions",
            "job_model_bindings",
            "task_visibility_versions",
        }
        with database.engine.connect() as connection:
            review_sql = connection.execute(
                text(
                    "SELECT sql FROM sqlite_master WHERE type='table' "
                    "AND name='review_runs'"
                )
            ).scalar_one()
            visibility_trigger = connection.execute(
                text(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' "
                    "AND name='review_runs_task_active'"
                )
            ).scalar_one()
        assert "'minimax'" in review_sql
        assert "Task is archived or deleted" in visibility_trigger
    finally:
        database.close()


def test_migrated_schema_matches_orm_metadata(tmp_path: Path) -> None:
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'schema-drift.db'}")
    try:
        database.create_schema()
        inspector = inspect(database.engine)
        actual_tables = set(inspector.get_table_names()) - {
            "_schema_migrations"
        }
        assert actual_tables == set(Base.metadata.tables)

        for table in Base.metadata.sorted_tables:
            actual_columns = {
                str(column["name"]): column
                for column in inspector.get_columns(table.name)
            }
            assert set(actual_columns) == {column.name for column in table.columns}
            for column in table.columns:
                assert actual_columns[column.name]["nullable"] == column.nullable

            actual_primary_key = tuple(
                inspector.get_pk_constraint(table.name).get(
                    "constrained_columns"
                )
                or ()
            )
            expected_primary_key = tuple(
                column.name for column in table.primary_key.columns
            )
            assert actual_primary_key == expected_primary_key

            with database.engine.connect() as connection:
                foreign_key_rows = connection.exec_driver_sql(
                    f'PRAGMA foreign_key_list("{table.name}")'
                ).mappings()
                actual_foreign_keys = {
                    (
                        (str(item["from"]),),
                        str(item["table"]),
                        (str(item["to"]),),
                        str(item["on_delete"] or "").upper(),
                    )
                    for item in foreign_key_rows
                }
            expected_foreign_keys = {
                (
                    (foreign_key.parent.name,),
                    foreign_key.column.table.name,
                    (foreign_key.column.name,),
                    str(foreign_key.ondelete or "").upper(),
                )
                for foreign_key in table.foreign_keys
            }
            assert actual_foreign_keys == expected_foreign_keys

            actual_indexes = {
                str(item["name"]): (
                    tuple(item.get("column_names") or ()),
                    bool(item.get("unique")),
                )
                for item in inspector.get_indexes(table.name)
                if item.get("name")
            }
            expected_indexes = {
                str(index.name): (
                    tuple(column.name for column in index.columns),
                    bool(index.unique),
                )
                for index in table.indexes
                if index.name
            }
            for name, definition in expected_indexes.items():
                assert actual_indexes.get(name) == definition
    finally:
        database.close()


def test_real_phase1_fixture_is_adopted_without_data_loss(tmp_path: Path) -> None:
    database_path = tmp_path / "phase1.db"
    _load_phase1_fixture(database_path)
    original_bytes = database_path.stat().st_size
    database = Database(f"sqlite+pysqlite:///{database_path}")

    try:
        report = database.create_schema()

        assert report.previous_revision is None
        assert report.current_revision == LATEST_REVISION
        assert report.applied == (
            "0002_provenance",
            "0003_jobs",
            "0004_artifacts_audit",
            "0005_scan_selection_date",
            "0006_provider_invocations",
            "0007_analysis_versions",
            "0008_final_score_versions",
            "0009_contribution_tasks",
            "0010_contribution_task_states",
            "0011_plan_versions",
            "0012_plan_revision_links",
            "0013_plan_locks",
            "0014_plan_approvals",
            "0015_plan_conversations",
            "0016_execution_attempts",
            "0017_execution_stage_runs",
            "0018_dependency_verify_inputs",
            "0019_execution_artifact_manifests",
            "0020_execution_workspace_disposals",
            "0021_review_runs",
            "0022_publish_intents",
            "0023_pull_request_events",
            "0024_task_side_states",
            "0025_product_experience",
            "0026_review_artifact_bindings",
            "0027_nvidia_agent_workflows",
            "0028_nvidia_review_runs",
            "0029_workbench",
            "0030_model_settings",
            "0031_task_visibility",
            LATEST_REVISION,
        )
        assert report.stamped == (BASELINE_REVISION,)
        assert database_path.stat().st_size >= original_bytes

        with database.engine.connect() as connection:
            repository = connection.execute(
                text(
                    "SELECT github_id, full_name, stars "
                    "FROM repositories WHERE id = 1"
                )
            ).one()
            opportunity = connection.execute(
                text(
                    "SELECT github_issue_id, title, score_total "
                    "FROM opportunities WHERE id = 1"
                )
            ).one()
            scan = connection.execute(
                text(
                    "SELECT id, candidate_count, eligible_count, selected_count "
                    "FROM scan_runs"
                )
            ).one()
            pick = connection.execute(
                text(
                    "SELECT selection_date, rank, opportunity_id, score_snapshot, "
                    "scan_run_id, snapshot_id, score_version_id, provenance_status "
                    "FROM daily_picks"
                )
            ).one()
            scan_provenance = connection.execute(
                text("SELECT provenance_status FROM scan_runs")
            ).scalar_one()

        assert tuple(repository) == (1001, "fixture/phase1", 1234)
        assert tuple(opportunity) == (2001, "Phase 1 fixture issue", 81.5)
        assert tuple(scan) == (
            "00000000-0000-0000-0000-000000000001",
            1,
            1,
            1,
        )
        assert tuple(pick) == (
            "2026-07-17",
            1,
            1,
            81.5,
            None,
            None,
            None,
            "legacy_unverified",
        )
        assert scan_provenance == "legacy_unverified"
    finally:
        database.close()


def test_phase1_backup_can_be_restored_and_upgraded_again(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "recoverable.db"
    backup_path = tmp_path / "recoverable.pre-migration.db"
    _load_phase1_fixture(database_path)
    shutil.copy2(database_path, backup_path)

    first = Database(f"sqlite+pysqlite:///{database_path}")
    try:
        first.create_schema()
    finally:
        first.close()

    shutil.copy2(backup_path, database_path)
    restored = Database(f"sqlite+pysqlite:///{database_path}")
    try:
        report = restored.create_schema()
        with restored.engine.connect() as connection:
            integrity = connection.execute(
                text("PRAGMA integrity_check")
            ).scalar_one()
            repository_count = connection.execute(
                text("SELECT COUNT(*) FROM repositories")
            ).scalar_one()
            opportunity_count = connection.execute(
                text("SELECT COUNT(*) FROM opportunities")
            ).scalar_one()

        assert report.stamped == (BASELINE_REVISION,)
        assert report.current_revision == LATEST_REVISION
        assert integrity == "ok"
        assert repository_count == 1
        assert opportunity_count == 1
    finally:
        restored.close()


def test_execution_artifact_manifest_upgrade_preserves_previous_rows(
    tmp_path: Path,
) -> None:
    database = Database(
        f"sqlite+pysqlite:///{tmp_path / 'from-0018.db'}"
    )
    try:
        previous = MigrationRunner(
            database.engine,
            migrations=tuple(
                item
                for item in MIGRATIONS
                if item.revision <= "0018_dependency_verify_inputs"
            ),
        ).upgrade()
        assert previous.current_revision == "0018_dependency_verify_inputs"
        with database.session() as session:
            job, _ = JobService(session).enqueue(
                kind="fixture",
                idempotency_key="pre-artifact-manifest-job",
                payload={"retained": True},
            )
            job_id = job.id

        report = database.create_schema()

        assert report.previous_revision == "0018_dependency_verify_inputs"
        assert report.applied == (
            "0019_execution_artifact_manifests",
            "0020_execution_workspace_disposals",
            "0021_review_runs",
            "0022_publish_intents",
            "0023_pull_request_events",
            "0024_task_side_states",
            "0025_product_experience",
            "0026_review_artifact_bindings",
            "0027_nvidia_agent_workflows",
            "0028_nvidia_review_runs",
            "0029_workbench",
            "0030_model_settings",
            "0031_task_visibility",
            LATEST_REVISION,
        )
        with database.session() as session:
            retained = JobService(session).get(job_id)
            assert retained.payload == {"retained": True}
            assert retained.state == "queued"
    finally:
        database.close()


def test_product_experience_and_review_binding_upgrade_preserves_rows(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'from-0024.db'}")
    try:
        previous = MigrationRunner(
            database.engine,
            migrations=tuple(
                item
                for item in MIGRATIONS
                if item.revision <= "0024_task_side_states"
            ),
        ).upgrade()
        assert previous.current_revision == "0024_task_side_states"
        with database.session() as session:
            job, _ = JobService(session).enqueue(
                kind="fixture",
                idempotency_key="retained-across-product-migration",
                payload={"retained": True},
            )
            job_id = job.id

        report = database.create_schema()

        assert report.previous_revision == "0024_task_side_states"
        assert report.applied == (
            "0025_product_experience",
            "0026_review_artifact_bindings",
            "0027_nvidia_agent_workflows",
            "0028_nvidia_review_runs",
            "0029_workbench",
            "0030_model_settings",
            "0031_task_visibility",
            LATEST_REVISION,
        )
        with database.session() as session:
            retained = JobService(session).get(job_id)
            assert retained.payload == {"retained": True}
        assert {
            "user_preference_versions",
            "workbench_events",
            "opportunity_disposition_versions",
            "in_app_notifications",
            "notification_reads",
        }.issubset(inspect(database.engine).get_table_names())
        with database.engine.connect() as connection:
            trigger_sql = connection.execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type = 'trigger' "
                    "AND name = 'review_runs_provenance_insert'"
                )
            ).scalar_one()
        assert "diff.artifact_id = NEW.diff_hash" in trigger_sql
        assert "tests.artifact_id = NEW.test_results_hash" in trigger_sql
    finally:
        database.close()


def test_partial_unversioned_schema_fails_closed(tmp_path: Path) -> None:
    database_path = tmp_path / "partial.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("CREATE TABLE repositories (id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()

    database = Database(f"sqlite+pysqlite:///{database_path}")
    try:
        with pytest.raises(MigrationError, match="does not match"):
            database.create_schema()

        assert "repositories" in inspect(database.engine).get_table_names()
        with database.engine.connect() as connection:
            columns = connection.execute(
                text("PRAGMA table_info(repositories)")
            ).all()
        assert [column[1] for column in columns] == ["id"]
    finally:
        database.close()


def test_applied_migration_checksum_mismatch_fails_closed(tmp_path: Path) -> None:
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'tampered.db'}")
    try:
        database.create_schema()
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE _schema_migrations "
                    "SET checksum = 'tampered' "
                    "WHERE revision = :revision"
                ),
                {"revision": BASELINE_REVISION},
            )

        with pytest.raises(MigrationError, match="Checksum mismatch"):
            database.create_schema()
    finally:
        database.close()


def test_every_registered_migration_has_recovery_instructions() -> None:
    runner = MigrationRunner(
        Database("sqlite+pysqlite:///:memory:").engine,
        migrations=MIGRATIONS,
    )

    assert runner.migrations
    assert all(migration.recovery.strip() for migration in runner.migrations)
