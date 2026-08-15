from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError


_STATEMENTS = (
    """
    CREATE TABLE plan_approvals (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        plan_lock_id VARCHAR(36) NOT NULL UNIQUE,
        plan_version_id VARCHAR(36) NOT NULL UNIQUE,
        task_id VARCHAR(36) NOT NULL,
        approved_state_version_id VARCHAR(36) NOT NULL UNIQUE,
        schema_version VARCHAR(32) NOT NULL,
        idempotency_key VARCHAR(128) NOT NULL UNIQUE,
        actor_type VARCHAR(40) NOT NULL,
        actor_id VARCHAR(128) NOT NULL,
        lock_hash VARCHAR(64) NOT NULL,
        plan_content_hash VARCHAR(64) NOT NULL,
        plan_record_hash VARCHAR(64) NOT NULL,
        prior_state_record_hash VARCHAR(64) NOT NULL,
        approved_state_record_hash VARCHAR(64) NOT NULL,
        approval_hash VARCHAR(64) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        CONSTRAINT ck_plan_approval_schema
            CHECK (schema_version = '1'),
        FOREIGN KEY(plan_lock_id)
            REFERENCES plan_locks (id) ON DELETE RESTRICT,
        FOREIGN KEY(plan_version_id)
            REFERENCES plan_versions (id) ON DELETE RESTRICT,
        FOREIGN KEY(task_id)
            REFERENCES contribution_tasks (id) ON DELETE RESTRICT,
        FOREIGN KEY(approved_state_version_id)
            REFERENCES contribution_task_state_versions (id)
            ON DELETE RESTRICT
    )
    """,
    (
        "CREATE UNIQUE INDEX ix_plan_approvals_plan_lock_id "
        "ON plan_approvals (plan_lock_id)"
    ),
    (
        "CREATE UNIQUE INDEX ix_plan_approvals_plan_version_id "
        "ON plan_approvals (plan_version_id)"
    ),
    "CREATE INDEX ix_plan_approvals_task_id ON plan_approvals (task_id)",
    (
        "CREATE UNIQUE INDEX ix_plan_approvals_approved_state_version_id "
        "ON plan_approvals (approved_state_version_id)"
    ),
    (
        "CREATE UNIQUE INDEX ix_plan_approvals_idempotency_key "
        "ON plan_approvals (idempotency_key)"
    ),
    (
        "CREATE UNIQUE INDEX ix_plan_approvals_approval_hash "
        "ON plan_approvals (approval_hash)"
    ),
    "CREATE INDEX ix_plan_approvals_created_at ON plan_approvals (created_at)",
    """
    CREATE TRIGGER plan_approvals_provenance_insert
    BEFORE INSERT ON plan_approvals
    WHEN NOT EXISTS (
        SELECT 1
        FROM plan_locks AS lock
        JOIN plan_versions AS plan
          ON plan.id = lock.plan_version_id
        JOIN contribution_task_state_versions AS prior
          ON prior.id = lock.task_state_version_id
        JOIN contribution_task_state_versions AS approved
          ON approved.id = NEW.approved_state_version_id
        WHERE lock.id = NEW.plan_lock_id
          AND lock.task_id = NEW.task_id
          AND lock.plan_version_id = NEW.plan_version_id
          AND lock.lock_hash = NEW.lock_hash
          AND lock.plan_content_hash = NEW.plan_content_hash
          AND lock.plan_record_hash = NEW.plan_record_hash
          AND plan.id = NEW.plan_version_id
          AND plan.task_id = NEW.task_id
          AND plan.content_hash = NEW.plan_content_hash
          AND plan.record_hash = NEW.plan_record_hash
          AND prior.task_id = NEW.task_id
          AND prior.to_state = 'planning'
          AND prior.record_hash = NEW.prior_state_record_hash
          AND approved.task_id = NEW.task_id
          AND approved.sequence = prior.sequence + 1
          AND approved.from_state = 'planning'
          AND approved.to_state = 'plan_approved'
          AND approved.reason_code = 'plan_approved'
          AND approved.previous_state_hash = prior.record_hash
          AND approved.record_hash = NEW.approved_state_record_hash
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid plan approval provenance');
    END
    """,
    """
    CREATE TRIGGER plan_approvals_no_update
    BEFORE UPDATE ON plan_approvals
    BEGIN
        SELECT RAISE(ABORT, 'plan approvals are immutable');
    END
    """,
    """
    CREATE TRIGGER plan_approvals_no_delete
    BEFORE DELETE ON plan_approvals
    BEGIN
        SELECT RAISE(ABORT, 'plan approvals are immutable');
    END
    """,
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0014_plan_approvals supports the SQLite MVP only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0014_plan_approvals",
    description="Add immutable exact-lock PlanApproval records",
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in _STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "This revision adds immutable PlanApproval records without rewriting "
        "plans, locks, or task states. Stop approval writers and preserve the "
        "SQLite file before upgrade. On failure restore it; never fabricate "
        "an actor, approved state, lock hash, or plan hash."
    ),
)
