from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError


_STATEMENTS = (
    """
    CREATE TABLE plan_locks (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        plan_version_id VARCHAR(36) NOT NULL UNIQUE,
        task_id VARCHAR(36) NOT NULL,
        task_state_version_id VARCHAR(36) NOT NULL,
        analysis_version_id VARCHAR(36) NOT NULL,
        snapshot_id VARCHAR(36) NOT NULL,
        schema_version VARCHAR(32) NOT NULL,
        idempotency_key VARCHAR(128) NOT NULL UNIQUE,
        base_commit_sha VARCHAR(64) NOT NULL,
        task_record_hash VARCHAR(64) NOT NULL,
        task_state_record_hash VARCHAR(64) NOT NULL,
        analysis_record_hash VARCHAR(64) NOT NULL,
        analysis_output_hash VARCHAR(64) NOT NULL,
        snapshot_inputs_hash VARCHAR(64) NOT NULL,
        provider_name VARCHAR(80) NOT NULL,
        adapter_version VARCHAR(80) NOT NULL,
        model_name VARCHAR(120) NOT NULL,
        model_version VARCHAR(120) NOT NULL,
        inspect_prompt_version VARCHAR(128) NOT NULL,
        inspect_policy_version VARCHAR(128) NOT NULL,
        inspect_output_schema_version VARCHAR(128) NOT NULL,
        analyze_prompt_version VARCHAR(128) NOT NULL,
        analyze_policy_version VARCHAR(128) NOT NULL,
        analyze_output_schema_version VARCHAR(128) NOT NULL,
        provider_contract_hash VARCHAR(64) NOT NULL,
        plan_content_hash VARCHAR(64) NOT NULL,
        plan_record_hash VARCHAR(64) NOT NULL,
        lock_hash VARCHAR(64) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        CONSTRAINT ck_plan_lock_schema
            CHECK (schema_version = '1'),
        CONSTRAINT ck_plan_lock_base_sha
            CHECK (
                (length(base_commit_sha) = 40 OR length(base_commit_sha) = 64)
                AND base_commit_sha NOT GLOB '*[^0-9a-f]*'
            ),
        FOREIGN KEY(plan_version_id)
            REFERENCES plan_versions (id) ON DELETE RESTRICT,
        FOREIGN KEY(task_id)
            REFERENCES contribution_tasks (id) ON DELETE RESTRICT,
        FOREIGN KEY(task_state_version_id)
            REFERENCES contribution_task_state_versions (id)
            ON DELETE RESTRICT,
        FOREIGN KEY(analysis_version_id)
            REFERENCES analysis_versions (id) ON DELETE RESTRICT,
        FOREIGN KEY(snapshot_id)
            REFERENCES opportunity_snapshots (id) ON DELETE RESTRICT
    )
    """,
    (
        "CREATE UNIQUE INDEX ix_plan_locks_plan_version_id "
        "ON plan_locks (plan_version_id)"
    ),
    "CREATE INDEX ix_plan_locks_task_id ON plan_locks (task_id)",
    (
        "CREATE INDEX ix_plan_locks_task_state_version_id "
        "ON plan_locks (task_state_version_id)"
    ),
    (
        "CREATE INDEX ix_plan_locks_analysis_version_id "
        "ON plan_locks (analysis_version_id)"
    ),
    "CREATE INDEX ix_plan_locks_snapshot_id ON plan_locks (snapshot_id)",
    (
        "CREATE UNIQUE INDEX ix_plan_locks_idempotency_key "
        "ON plan_locks (idempotency_key)"
    ),
    "CREATE UNIQUE INDEX ix_plan_locks_lock_hash ON plan_locks (lock_hash)",
    "CREATE INDEX ix_plan_locks_created_at ON plan_locks (created_at)",
    """
    CREATE TRIGGER plan_locks_provenance_insert
    BEFORE INSERT ON plan_locks
    WHEN NOT EXISTS (
        SELECT 1
        FROM plan_versions AS plan
        JOIN contribution_tasks AS task
          ON task.id = plan.task_id
        JOIN contribution_task_state_versions AS state
          ON state.id = NEW.task_state_version_id
        JOIN analysis_versions AS analysis
          ON analysis.id = task.analysis_version_id
        JOIN opportunity_snapshots AS snapshot
          ON snapshot.id = analysis.snapshot_id
        WHERE plan.id = NEW.plan_version_id
          AND plan.task_id = NEW.task_id
          AND plan.record_hash = NEW.plan_record_hash
          AND plan.content_hash = NEW.plan_content_hash
          AND task.record_hash = NEW.task_record_hash
          AND state.task_id = task.id
          AND state.record_hash = NEW.task_state_record_hash
          AND state.to_state = 'planning'
          AND analysis.id = NEW.analysis_version_id
          AND analysis.snapshot_id = NEW.snapshot_id
          AND analysis.record_hash = NEW.analysis_record_hash
          AND analysis.analysis_output_hash = NEW.analysis_output_hash
          AND analysis.snapshot_inputs_hash = NEW.snapshot_inputs_hash
          AND analysis.provider_name = NEW.provider_name
          AND analysis.adapter_version = NEW.adapter_version
          AND analysis.model_name = NEW.model_name
          AND analysis.model_version = NEW.model_version
          AND analysis.inspect_prompt_version = NEW.inspect_prompt_version
          AND analysis.inspect_policy_version = NEW.inspect_policy_version
          AND analysis.inspect_output_schema_version
              = NEW.inspect_output_schema_version
          AND analysis.analyze_prompt_version = NEW.analyze_prompt_version
          AND analysis.analyze_policy_version = NEW.analyze_policy_version
          AND analysis.analyze_output_schema_version
              = NEW.analyze_output_schema_version
          AND snapshot.inputs_hash = NEW.snapshot_inputs_hash
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid plan lock provenance');
    END
    """,
    """
    CREATE TRIGGER plan_locks_no_update
    BEFORE UPDATE ON plan_locks
    BEGIN
        SELECT RAISE(ABORT, 'plan locks are immutable');
    END
    """,
    """
    CREATE TRIGGER plan_locks_no_delete
    BEFORE DELETE ON plan_locks
    BEGIN
        SELECT RAISE(ABORT, 'plan locks are immutable');
    END
    """,
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0013_plan_locks supports the SQLite MVP only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0013_plan_locks",
    description="Add immutable exact-input PlanLock records",
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in _STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "This revision adds immutable pre-approval lock records without "
        "approving a plan or changing task state. Stop plan writers and "
        "preserve the SQLite file before upgrade. On failure restore it; never "
        "fabricate a base SHA, rewrite Provider/Policy versions, or disable "
        "provenance triggers to force a lock."
    ),
)
