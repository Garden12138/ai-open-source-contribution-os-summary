from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError


_STATEMENTS = (
    """
    CREATE TABLE user_preference_versions (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        version INTEGER NOT NULL UNIQUE,
        schema_version VARCHAR(32) NOT NULL,
        primary_goal VARCHAR(32) NOT NULL,
        preferred_languages JSON NOT NULL,
        weekly_hours INTEGER NOT NULL,
        minimum_bounty_usd FLOAT NOT NULL,
        auto_scan_enabled BOOLEAN NOT NULL,
        auto_scan_local_time VARCHAR(5) NOT NULL,
        record_hash VARCHAR(64) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        CONSTRAINT ck_user_preference_schema CHECK (schema_version = '1'),
        CONSTRAINT ck_user_preference_version CHECK (version >= 1),
        CONSTRAINT ck_user_preference_goal CHECK (
            primary_goal IN (
                'balanced', 'bounty', 'impact', 'quick_merge', 'learning'
            )
        ),
        CONSTRAINT ck_user_preference_weekly_hours CHECK (
            weekly_hours >= 1 AND weekly_hours <= 40
        ),
        CONSTRAINT ck_user_preference_minimum_bounty CHECK (
            minimum_bounty_usd >= 0
        )
    )
    """,
    "CREATE INDEX ix_user_preference_versions_created_at ON user_preference_versions (created_at)",
    "CREATE INDEX ix_user_preference_versions_primary_goal ON user_preference_versions (primary_goal)",
    """
    CREATE TABLE opportunity_disposition_versions (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        opportunity_id INTEGER NOT NULL,
        preference_version_id VARCHAR(36),
        sequence INTEGER NOT NULL,
        state VARCHAR(24) NOT NULL,
        reason_code VARCHAR(80),
        reminder_at DATETIME,
        record_hash VARCHAR(64) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        CONSTRAINT uq_opportunity_disposition_sequence UNIQUE (
            opportunity_id, sequence
        ),
        CONSTRAINT ck_opportunity_disposition_sequence CHECK (sequence >= 1),
        CONSTRAINT ck_opportunity_disposition_state CHECK (
            state IN ('shortlisted', 'dismissed', 'neutral')
        ),
        FOREIGN KEY(opportunity_id) REFERENCES opportunities (id) ON DELETE RESTRICT,
        FOREIGN KEY(preference_version_id)
            REFERENCES user_preference_versions (id) ON DELETE RESTRICT
    )
    """,
    "CREATE INDEX ix_opportunity_disposition_versions_opportunity_id ON opportunity_disposition_versions (opportunity_id)",
    "CREATE INDEX ix_opportunity_disposition_versions_preference_version_id ON opportunity_disposition_versions (preference_version_id)",
    "CREATE INDEX ix_opportunity_disposition_versions_state ON opportunity_disposition_versions (state)",
    "CREATE INDEX ix_opportunity_disposition_versions_reminder_at ON opportunity_disposition_versions (reminder_at)",
    "CREATE INDEX ix_opportunity_disposition_versions_created_at ON opportunity_disposition_versions (created_at)",
    """
    CREATE TABLE in_app_notifications (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        kind VARCHAR(40) NOT NULL,
        opportunity_id INTEGER,
        scan_run_id VARCHAR(36),
        dedupe_key VARCHAR(160) NOT NULL UNIQUE,
        title VARCHAR(300) NOT NULL,
        message VARCHAR(1000) NOT NULL,
        created_at DATETIME NOT NULL,
        CONSTRAINT ck_in_app_notification_kind CHECK (
            kind IN ('new_match', 'shortlist_updated', 'reminder_due')
        ),
        FOREIGN KEY(opportunity_id) REFERENCES opportunities (id) ON DELETE RESTRICT,
        FOREIGN KEY(scan_run_id) REFERENCES scan_runs (id) ON DELETE RESTRICT
    )
    """,
    "CREATE INDEX ix_in_app_notifications_kind ON in_app_notifications (kind)",
    "CREATE INDEX ix_in_app_notifications_opportunity_id ON in_app_notifications (opportunity_id)",
    "CREATE INDEX ix_in_app_notifications_scan_run_id ON in_app_notifications (scan_run_id)",
    "CREATE INDEX ix_in_app_notifications_created_at ON in_app_notifications (created_at)",
    """
    CREATE TABLE notification_reads (
        notification_id VARCHAR(36) NOT NULL PRIMARY KEY,
        read_at DATETIME NOT NULL,
        FOREIGN KEY(notification_id)
            REFERENCES in_app_notifications (id) ON DELETE RESTRICT
    )
    """,
    "CREATE INDEX ix_notification_reads_read_at ON notification_reads (read_at)",
    """
    CREATE TRIGGER user_preference_versions_no_update
    BEFORE UPDATE ON user_preference_versions
    BEGIN
        SELECT RAISE(ABORT, 'user preference versions are immutable');
    END
    """,
    """
    CREATE TRIGGER user_preference_versions_no_delete
    BEFORE DELETE ON user_preference_versions
    BEGIN
        SELECT RAISE(ABORT, 'user preference versions are immutable');
    END
    """,
    """
    CREATE TRIGGER opportunity_disposition_versions_no_update
    BEFORE UPDATE ON opportunity_disposition_versions
    BEGIN
        SELECT RAISE(ABORT, 'opportunity disposition versions are immutable');
    END
    """,
    """
    CREATE TRIGGER opportunity_disposition_versions_no_delete
    BEFORE DELETE ON opportunity_disposition_versions
    BEGIN
        SELECT RAISE(ABORT, 'opportunity disposition versions are immutable');
    END
    """,
    """
    CREATE TRIGGER in_app_notifications_no_update
    BEFORE UPDATE ON in_app_notifications
    BEGIN
        SELECT RAISE(ABORT, 'in-app notifications are immutable');
    END
    """,
    """
    CREATE TRIGGER in_app_notifications_no_delete
    BEFORE DELETE ON in_app_notifications
    BEGIN
        SELECT RAISE(ABORT, 'in-app notifications are immutable');
    END
    """,
    """
    CREATE TRIGGER notification_reads_no_update
    BEFORE UPDATE ON notification_reads
    BEGIN
        SELECT RAISE(ABORT, 'notification reads are immutable');
    END
    """,
    """
    CREATE TRIGGER notification_reads_no_delete
    BEFORE DELETE ON notification_reads
    BEGIN
        SELECT RAISE(ABORT, 'notification reads are immutable');
    END
    """,
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0025_product_experience supports the SQLite MVP only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0025_product_experience",
    description=(
        "Add immutable local preferences, opportunity decisions, and in-app "
        "notification state"
    ),
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in _STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "Stop API and worker processes and preserve the SQLite database before "
        "upgrade. This additive revision does not rewrite existing discovery or "
        "contribution rows. On failure restore the backup; the four new tables "
        "may be ignored by an older binary but must not be partially dropped."
    ),
)
