from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError


_STATEMENTS = (
    """
    CREATE TABLE final_score_versions (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        snapshot_id VARCHAR(36) NOT NULL,
        rule_score_version_id VARCHAR(36) NOT NULL,
        analysis_version_id VARCHAR(36) NOT NULL,
        algorithm_version VARCHAR(64) NOT NULL,
        schema_version VARCHAR(32) NOT NULL,
        input_hash VARCHAR(64) NOT NULL,
        calibration_hash VARCHAR(64) NOT NULL,
        output_hash VARCHAR(64) NOT NULL UNIQUE,
        score_total FLOAT NOT NULL,
        score_components JSON NOT NULL,
        risk_penalty FLOAT NOT NULL,
        risk_reasons JSON NOT NULL,
        rationale TEXT NOT NULL,
        cited_evidence_ids JSON NOT NULL,
        created_at DATETIME NOT NULL,
        CONSTRAINT uq_final_score_analysis_algorithm
            UNIQUE (analysis_version_id, algorithm_version),
        CONSTRAINT ck_final_score_schema
            CHECK (schema_version = '1'),
        CONSTRAINT ck_final_score_total
            CHECK (score_total >= 0 AND score_total <= 100),
        CONSTRAINT ck_final_score_risk_penalty
            CHECK (risk_penalty >= 0),
        FOREIGN KEY(snapshot_id)
            REFERENCES opportunity_snapshots (id) ON DELETE RESTRICT,
        FOREIGN KEY(rule_score_version_id)
            REFERENCES score_versions (id) ON DELETE RESTRICT,
        FOREIGN KEY(analysis_version_id)
            REFERENCES analysis_versions (id) ON DELETE RESTRICT
    )
    """,
    (
        "CREATE INDEX ix_final_score_versions_snapshot_id "
        "ON final_score_versions (snapshot_id)"
    ),
    (
        "CREATE INDEX ix_final_score_versions_rule_score_version_id "
        "ON final_score_versions (rule_score_version_id)"
    ),
    (
        "CREATE INDEX ix_final_score_versions_analysis_version_id "
        "ON final_score_versions (analysis_version_id)"
    ),
    (
        "CREATE UNIQUE INDEX ix_final_score_versions_output_hash "
        "ON final_score_versions (output_hash)"
    ),
    "CREATE INDEX ix_final_score_versions_created_at ON final_score_versions (created_at)",
    """
    CREATE TRIGGER final_score_versions_provenance_insert
    BEFORE INSERT ON final_score_versions
    WHEN NOT EXISTS (
        SELECT 1
        FROM analysis_versions AS analysis
        JOIN score_versions AS score
          ON score.id = analysis.score_version_id
        WHERE analysis.id = NEW.analysis_version_id
          AND analysis.snapshot_id = NEW.snapshot_id
          AND score.id = NEW.rule_score_version_id
          AND score.snapshot_id = NEW.snapshot_id
          AND score.output_hash = analysis.score_output_hash
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid final score provenance');
    END
    """,
    """
    CREATE TRIGGER final_score_versions_no_update
    BEFORE UPDATE ON final_score_versions
    BEGIN
        SELECT RAISE(ABORT, 'final score versions are immutable');
    END
    """,
    """
    CREATE TRIGGER final_score_versions_no_delete
    BEFORE DELETE ON final_score_versions
    BEGIN
        SELECT RAISE(ABORT, 'final score versions are immutable');
    END
    """,
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0008_final_score_versions supports the SQLite MVP only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0008_final_score_versions",
    description="Add immutable AI-calibrated FinalScoreVersion records",
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in _STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "This revision adds a version table and immutable provenance triggers "
        "without changing any rule ScoreVersion. Stop writers and preserve the "
        "SQLite file before upgrade. On failure restore it; never overwrite a "
        "rule score, disable the triggers, or fabricate an AnalysisVersion to "
        "force a final score insert."
    ),
)
