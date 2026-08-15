from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError


_STATEMENTS = (
    """
    CREATE TABLE opportunity_snapshots (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        scan_run_id VARCHAR(36) NOT NULL,
        opportunity_id INTEGER NOT NULL,
        schema_version VARCHAR(32) NOT NULL,
        inputs_hash VARCHAR(64) NOT NULL UNIQUE,
        issue_data JSON NOT NULL,
        repository_data JSON NOT NULL,
        source_queries JSON NOT NULL,
        rule_config JSON NOT NULL,
        filter_eligible BOOLEAN NOT NULL,
        filter_reasons JSON NOT NULL,
        captured_at DATETIME NOT NULL,
        created_at DATETIME NOT NULL,
        CONSTRAINT uq_snapshot_scan_opportunity
            UNIQUE (scan_run_id, opportunity_id),
        FOREIGN KEY(scan_run_id) REFERENCES scan_runs (id) ON DELETE RESTRICT,
        FOREIGN KEY(opportunity_id) REFERENCES opportunities (id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX ix_opportunity_snapshots_scan_run_id
        ON opportunity_snapshots (scan_run_id)
    """,
    """
    CREATE INDEX ix_opportunity_snapshots_opportunity_id
        ON opportunity_snapshots (opportunity_id)
    """,
    """
    CREATE UNIQUE INDEX ix_opportunity_snapshots_inputs_hash
        ON opportunity_snapshots (inputs_hash)
    """,
    """
    CREATE TABLE score_versions (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        snapshot_id VARCHAR(36) NOT NULL,
        algorithm_version VARCHAR(64) NOT NULL,
        schema_version VARCHAR(32) NOT NULL,
        inputs_hash VARCHAR(64) NOT NULL,
        output_hash VARCHAR(64) NOT NULL UNIQUE,
        score_total FLOAT NOT NULL CHECK (score_total >= 0 AND score_total <= 100),
        score_components JSON NOT NULL,
        risk_penalty FLOAT NOT NULL CHECK (risk_penalty >= 0),
        risk_reasons JSON NOT NULL,
        has_bounty BOOLEAN NOT NULL,
        bounty_amount_usd FLOAT,
        is_strategic BOOLEAN NOT NULL,
        is_tech_match BOOLEAN NOT NULL,
        created_at DATETIME NOT NULL,
        CONSTRAINT uq_score_snapshot_algorithm
            UNIQUE (snapshot_id, algorithm_version),
        FOREIGN KEY(snapshot_id)
            REFERENCES opportunity_snapshots (id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX ix_score_versions_snapshot_id
        ON score_versions (snapshot_id)
    """,
    """
    CREATE UNIQUE INDEX ix_score_versions_output_hash
        ON score_versions (output_hash)
    """,
    """
    ALTER TABLE scan_runs
        ADD COLUMN provenance_status VARCHAR(32) NOT NULL
        DEFAULT 'legacy_unverified'
        CHECK (provenance_status IN ('verified', 'legacy_unverified'))
    """,
    """
    CREATE INDEX ix_scan_runs_provenance_status
        ON scan_runs (provenance_status)
    """,
    """
    ALTER TABLE daily_picks
        ADD COLUMN scan_run_id VARCHAR(36)
        REFERENCES scan_runs (id) ON DELETE RESTRICT
    """,
    """
    ALTER TABLE daily_picks
        ADD COLUMN snapshot_id VARCHAR(36)
        REFERENCES opportunity_snapshots (id) ON DELETE RESTRICT
    """,
    """
    ALTER TABLE daily_picks
        ADD COLUMN score_version_id VARCHAR(36)
        REFERENCES score_versions (id) ON DELETE RESTRICT
    """,
    """
    ALTER TABLE daily_picks
        ADD COLUMN provenance_status VARCHAR(32) NOT NULL
        DEFAULT 'legacy_unverified'
        CHECK (provenance_status IN ('verified', 'legacy_unverified'))
    """,
    """
    CREATE INDEX ix_daily_picks_scan_run_id
        ON daily_picks (scan_run_id)
    """,
    """
    CREATE INDEX ix_daily_picks_snapshot_id
        ON daily_picks (snapshot_id)
    """,
    """
    CREATE INDEX ix_daily_picks_score_version_id
        ON daily_picks (score_version_id)
    """,
    """
    CREATE INDEX ix_daily_picks_provenance_status
        ON daily_picks (provenance_status)
    """,
    """
    CREATE TRIGGER opportunity_snapshots_no_update
    BEFORE UPDATE ON opportunity_snapshots
    BEGIN
        SELECT RAISE(ABORT, 'opportunity snapshots are immutable');
    END
    """,
    """
    CREATE TRIGGER opportunity_snapshots_no_delete
    BEFORE DELETE ON opportunity_snapshots
    BEGIN
        SELECT RAISE(ABORT, 'opportunity snapshots are immutable');
    END
    """,
    """
    CREATE TRIGGER score_versions_no_update
    BEFORE UPDATE ON score_versions
    BEGIN
        SELECT RAISE(ABORT, 'score versions are immutable');
    END
    """,
    """
    CREATE TRIGGER score_versions_no_delete
    BEFORE DELETE ON score_versions
    BEGIN
        SELECT RAISE(ABORT, 'score versions are immutable');
    END
    """,
    """
    CREATE TRIGGER score_versions_match_snapshot
    BEFORE INSERT ON score_versions
    WHEN NOT EXISTS (
        SELECT 1
        FROM opportunity_snapshots AS snapshot
        WHERE snapshot.id = NEW.snapshot_id
          AND snapshot.inputs_hash = NEW.inputs_hash
    )
    BEGIN
        SELECT RAISE(ABORT, 'score input hash does not match snapshot');
    END
    """,
    """
    CREATE TRIGGER daily_picks_provenance_insert
    BEFORE INSERT ON daily_picks
    WHEN (
        NEW.provenance_status = 'legacy_unverified'
        AND (
            NEW.scan_run_id IS NOT NULL
            OR NEW.snapshot_id IS NOT NULL
            OR NEW.score_version_id IS NOT NULL
        )
    ) OR (
        NEW.provenance_status = 'verified'
        AND (
            NEW.scan_run_id IS NULL
            OR NEW.snapshot_id IS NULL
            OR NEW.score_version_id IS NULL
            OR NOT EXISTS (
                SELECT 1
                FROM opportunity_snapshots AS snapshot
                JOIN score_versions AS score
                  ON score.snapshot_id = snapshot.id
                WHERE snapshot.id = NEW.snapshot_id
                  AND score.id = NEW.score_version_id
                  AND snapshot.scan_run_id = NEW.scan_run_id
                  AND snapshot.opportunity_id = NEW.opportunity_id
            )
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid daily pick provenance');
    END
    """,
    """
    CREATE TRIGGER daily_picks_provenance_update
    BEFORE UPDATE OF opportunity_id, scan_run_id, snapshot_id,
                     score_version_id, provenance_status
    ON daily_picks
    WHEN (
        NEW.provenance_status = 'legacy_unverified'
        AND (
            NEW.scan_run_id IS NOT NULL
            OR NEW.snapshot_id IS NOT NULL
            OR NEW.score_version_id IS NOT NULL
        )
    ) OR (
        NEW.provenance_status = 'verified'
        AND (
            NEW.scan_run_id IS NULL
            OR NEW.snapshot_id IS NULL
            OR NEW.score_version_id IS NULL
            OR NOT EXISTS (
                SELECT 1
                FROM opportunity_snapshots AS snapshot
                JOIN score_versions AS score
                  ON score.snapshot_id = snapshot.id
                WHERE snapshot.id = NEW.snapshot_id
                  AND score.id = NEW.score_version_id
                  AND snapshot.scan_run_id = NEW.scan_run_id
                  AND snapshot.opportunity_id = NEW.opportunity_id
            )
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid daily pick provenance');
    END
    """,
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0002_provenance currently supports the SQLite MVP only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0002_provenance",
    description="Add immutable opportunity and rule-score provenance",
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in _STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "This revision preserves Phase 1 rows and labels their scan and pick "
        "records legacy_unverified. Before upgrading, stop writers and preserve "
        "the SQLite file. On failure, retain the error and restore the untouched "
        "file; do not remove migration rows or immutable triggers manually."
    ),
)
