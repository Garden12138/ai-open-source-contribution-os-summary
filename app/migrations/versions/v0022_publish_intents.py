from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError


_STATEMENTS = (
    """
    CREATE TABLE publish_intents (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        review_run_id VARCHAR(36) NOT NULL UNIQUE,
        execution_attempt_id VARCHAR(36) NOT NULL,
        task_id VARCHAR(36) NOT NULL,
        schema_version VARCHAR(32) NOT NULL,
        idempotency_key VARCHAR(128) NOT NULL UNIQUE,
        actor_type VARCHAR(40) NOT NULL,
        actor_id VARCHAR(128) NOT NULL,
        upstream_repository VARCHAR(255) NOT NULL,
        base_commit_sha VARCHAR(64) NOT NULL,
        head_branch VARCHAR(200) NOT NULL,
        head_commit_sha VARCHAR(64) NOT NULL,
        diff_hash VARCHAR(64) NOT NULL,
        test_results_hash VARCHAR(64) NOT NULL,
        review_record_hash VARCHAR(64) NOT NULL,
        title VARCHAR(200) NOT NULL,
        body VARCHAR(8000) NOT NULL,
        allowed_actions JSON NOT NULL,
        confirmation_nonce VARCHAR(64) NOT NULL,
        expires_at DATETIME NOT NULL,
        record_hash VARCHAR(64) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        CONSTRAINT ck_publish_intent_schema
            CHECK (schema_version = '1'),
        FOREIGN KEY(review_run_id)
            REFERENCES review_runs (id) ON DELETE RESTRICT,
        FOREIGN KEY(execution_attempt_id)
            REFERENCES execution_attempts (id) ON DELETE RESTRICT,
        FOREIGN KEY(task_id)
            REFERENCES contribution_tasks (id) ON DELETE RESTRICT
    )
    """,
    "CREATE INDEX ix_publish_intents_task_id ON publish_intents (task_id)",
    (
        "CREATE INDEX ix_publish_intents_execution_attempt_id "
        "ON publish_intents (execution_attempt_id)"
    ),
    (
        "CREATE INDEX ix_publish_intents_created_at "
        "ON publish_intents (created_at)"
    ),
    (
        "CREATE UNIQUE INDEX ix_publish_intents_review_run_id "
        "ON publish_intents (review_run_id)"
    ),
    (
        "CREATE UNIQUE INDEX ix_publish_intents_idempotency_key "
        "ON publish_intents (idempotency_key)"
    ),
    (
        "CREATE UNIQUE INDEX ix_publish_intents_record_hash "
        "ON publish_intents (record_hash)"
    ),
    """
    CREATE TABLE draft_pull_requests (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        publish_intent_id VARCHAR(36) NOT NULL UNIQUE,
        task_id VARCHAR(36) NOT NULL,
        schema_version VARCHAR(32) NOT NULL,
        provider VARCHAR(40) NOT NULL,
        number INTEGER NOT NULL,
        html_url VARCHAR(500) NOT NULL,
        head_branch VARCHAR(200) NOT NULL,
        base_commit_sha VARCHAR(64) NOT NULL,
        head_commit_sha VARCHAR(64) NOT NULL,
        diff_hash VARCHAR(64) NOT NULL,
        review_record_hash VARCHAR(64) NOT NULL,
        record_hash VARCHAR(64) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        CONSTRAINT ck_draft_pull_request_schema
            CHECK (schema_version = '1'),
        CONSTRAINT ck_draft_pull_request_provider
            CHECK (provider = 'fake'),
        CONSTRAINT ck_draft_pull_request_number
            CHECK (number >= 1),
        FOREIGN KEY(publish_intent_id)
            REFERENCES publish_intents (id) ON DELETE RESTRICT,
        FOREIGN KEY(task_id)
            REFERENCES contribution_tasks (id) ON DELETE RESTRICT
    )
    """,
    (
        "CREATE INDEX ix_draft_pull_requests_task_id "
        "ON draft_pull_requests (task_id)"
    ),
    (
        "CREATE INDEX ix_draft_pull_requests_created_at "
        "ON draft_pull_requests (created_at)"
    ),
    (
        "CREATE UNIQUE INDEX ix_draft_pull_requests_publish_intent_id "
        "ON draft_pull_requests (publish_intent_id)"
    ),
    (
        "CREATE UNIQUE INDEX ix_draft_pull_requests_record_hash "
        "ON draft_pull_requests (record_hash)"
    ),
    """
    CREATE TABLE publish_confirmations (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        publish_intent_id VARCHAR(36) NOT NULL UNIQUE,
        draft_pull_request_id VARCHAR(36) NOT NULL UNIQUE,
        schema_version VARCHAR(32) NOT NULL,
        actor_type VARCHAR(40) NOT NULL,
        actor_id VARCHAR(128) NOT NULL,
        confirmation_nonce VARCHAR(64) NOT NULL,
        draft_pr_state_version_id VARCHAR(36) NOT NULL UNIQUE,
        draft_pr_state_record_hash VARCHAR(64) NOT NULL,
        record_hash VARCHAR(64) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        CONSTRAINT ck_publish_confirmation_schema
            CHECK (schema_version = '1'),
        FOREIGN KEY(publish_intent_id)
            REFERENCES publish_intents (id) ON DELETE RESTRICT,
        FOREIGN KEY(draft_pull_request_id)
            REFERENCES draft_pull_requests (id) ON DELETE RESTRICT,
        FOREIGN KEY(draft_pr_state_version_id)
            REFERENCES contribution_task_state_versions (id)
            ON DELETE RESTRICT
    )
    """,
    (
        "CREATE INDEX ix_publish_confirmations_created_at "
        "ON publish_confirmations (created_at)"
    ),
    (
        "CREATE UNIQUE INDEX ix_publish_confirmations_publish_intent_id "
        "ON publish_confirmations (publish_intent_id)"
    ),
    (
        "CREATE UNIQUE INDEX ix_publish_confirmations_draft_pull_request_id "
        "ON publish_confirmations (draft_pull_request_id)"
    ),
    (
        "CREATE UNIQUE INDEX "
        "ix_publish_confirmations_draft_pr_state_version_id "
        "ON publish_confirmations (draft_pr_state_version_id)"
    ),
    (
        "CREATE UNIQUE INDEX ix_publish_confirmations_record_hash "
        "ON publish_confirmations (record_hash)"
    ),
    """
    CREATE TRIGGER publish_intents_provenance_insert
    BEFORE INSERT ON publish_intents
    WHEN NOT (
        json_valid(NEW.allowed_actions)
        AND json_type(NEW.allowed_actions) = 'array'
        AND EXISTS (
            SELECT 1
            FROM review_runs AS review
            JOIN execution_attempts AS attempt
              ON attempt.id = review.execution_attempt_id
            WHERE review.id = NEW.review_run_id
              AND review.execution_attempt_id = NEW.execution_attempt_id
              AND review.task_id = NEW.task_id
              AND review.record_hash = NEW.review_record_hash
              AND review.verdict = 'pass'
              AND review.diff_hash = NEW.diff_hash
              AND review.test_results_hash = NEW.test_results_hash
              AND review.base_commit_sha = NEW.base_commit_sha
              AND attempt.repository_full_name = NEW.upstream_repository
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid publish intent provenance');
    END
    """,
    """
    CREATE TRIGGER publish_intents_no_update
    BEFORE UPDATE ON publish_intents
    BEGIN
        SELECT RAISE(ABORT, 'publish intents are immutable');
    END
    """,
    """
    CREATE TRIGGER publish_intents_no_delete
    BEFORE DELETE ON publish_intents
    BEGIN
        SELECT RAISE(ABORT, 'publish intents are immutable');
    END
    """,
    """
    CREATE TRIGGER draft_pull_requests_no_update
    BEFORE UPDATE ON draft_pull_requests
    BEGIN
        SELECT RAISE(ABORT, 'draft pull requests are immutable');
    END
    """,
    """
    CREATE TRIGGER draft_pull_requests_no_delete
    BEFORE DELETE ON draft_pull_requests
    BEGIN
        SELECT RAISE(ABORT, 'draft pull requests are immutable');
    END
    """,
    """
    CREATE TRIGGER publish_confirmations_provenance_insert
    BEFORE INSERT ON publish_confirmations
    WHEN NOT EXISTS (
        SELECT 1
        FROM publish_intents AS intent
        JOIN draft_pull_requests AS draft
          ON draft.id = NEW.draft_pull_request_id
        JOIN contribution_task_state_versions AS draft_state
          ON draft_state.id = NEW.draft_pr_state_version_id
        WHERE intent.id = NEW.publish_intent_id
          AND intent.confirmation_nonce = NEW.confirmation_nonce
          AND intent.actor_id = NEW.actor_id
          AND draft.publish_intent_id = intent.id
          AND draft.task_id = intent.task_id
          AND draft_state.task_id = intent.task_id
          AND draft_state.from_state = 'ready'
          AND draft_state.to_state = 'draft_pr'
          AND draft_state.record_hash = NEW.draft_pr_state_record_hash
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid publish confirmation provenance');
    END
    """,
    """
    CREATE TRIGGER publish_confirmations_no_update
    BEFORE UPDATE ON publish_confirmations
    BEGIN
        SELECT RAISE(ABORT, 'publish confirmations are immutable');
    END
    """,
    """
    CREATE TRIGGER publish_confirmations_no_delete
    BEFORE DELETE ON publish_confirmations
    BEGIN
        SELECT RAISE(ABORT, 'publish confirmations are immutable');
    END
    """,
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0022_publish_intents supports the SQLite MVP only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0022_publish_intents",
    description="Add immutable PublishIntent, confirmation, and Draft PR records",
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in _STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "Stop publication writers and preserve the SQLite file before "
        "upgrade. This additive revision never writes to GitHub. On "
        "failure restore the backup; never edit an intent, confirmation, "
        "or Draft PR row, and never mark a confirmation without the "
        "exact nonce and ready-to-draft_pr state transition."
    ),
)
