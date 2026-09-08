from sqlalchemy import Connection
from app.migrations.core import Migration

STATEMENTS = (
    """CREATE TABLE workbench_events (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    task_id VARCHAR(36) NOT NULL REFERENCES contribution_tasks(id) ON DELETE RESTRICT,
    sequence BIGINT NOT NULL, kind VARCHAR(64) NOT NULL, actor_id VARCHAR(128) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL UNIQUE,
    job_id VARCHAR(36) REFERENCES jobs(id) ON DELETE RESTRICT,
    plan_version_id VARCHAR(36) REFERENCES plan_versions(id) ON DELETE RESTRICT,
    payload JSON NOT NULL, payload_hash VARCHAR(64) NOT NULL,
    previous_hash VARCHAR(64), record_hash VARCHAR(64) NOT NULL UNIQUE,
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_workbench_sequence UNIQUE(task_id, sequence),
    CONSTRAINT ck_workbench_sequence CHECK(sequence >= 1))""",
    "CREATE INDEX ix_workbench_events_task_id ON workbench_events(task_id)",
    """CREATE TRIGGER workbench_events_no_update BEFORE UPDATE ON workbench_events
    BEGIN SELECT RAISE(ABORT, 'Workbench events are immutable'); END""",
    """CREATE TRIGGER workbench_events_no_delete BEFORE DELETE ON workbench_events
    BEGIN SELECT RAISE(ABORT, 'Workbench events are immutable'); END""",
    """CREATE TRIGGER workbench_events_chain BEFORE INSERT ON workbench_events
    WHEN (NEW.sequence = 1 AND NEW.previous_hash IS NOT NULL)
    OR (NEW.sequence > 1 AND NOT EXISTS (SELECT 1 FROM workbench_events
        WHERE task_id = NEW.task_id AND sequence = NEW.sequence - 1
        AND record_hash = NEW.previous_hash))
    OR (NEW.plan_version_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM plan_versions
        WHERE id = NEW.plan_version_id AND task_id = NEW.task_id))
    BEGIN SELECT RAISE(ABORT, 'Workbench predecessor or plan mismatch'); END""",
)


STATEMENTS += (
    "DROP TRIGGER contribution_task_state_legal_insert",
    """CREATE TRIGGER contribution_task_state_legal_insert
    BEFORE INSERT ON contribution_task_state_versions
    WHEN NEW.sequence > 1 AND NOT (
        (NEW.from_state = 'planning' AND NEW.to_state = 'plan_approved')
        OR (
            NEW.from_state = 'plan_approved'
            AND NEW.to_state IN ('planning', 'executing')
        )
        OR (NEW.from_state = 'executing' AND NEW.to_state = 'reviewing')
        OR (NEW.from_state IN ('executing', 'reviewing') AND NEW.to_state = 'planning' AND NEW.reason_code = 'user_replan')
        OR (
            NEW.from_state = 'reviewing'
            AND NEW.to_state IN ('executing', 'ready')
        )
        OR (
            NEW.from_state = 'ready'
            AND NEW.to_state IN ('planning', 'draft_pr')
        )
        OR (
            NEW.from_state = 'draft_pr'
            AND NEW.to_state IN ('changes_requested', 'merged')
        )
        OR (
            NEW.from_state = 'changes_requested'
            AND NEW.to_state IN ('planning', 'executing')
        )
        OR (NEW.from_state = 'merged' AND NEW.to_state = 'rewarded')
    )
    BEGIN
        SELECT RAISE(ABORT, 'illegal contribution task state transition');
    END""",
    "DROP TRIGGER execution_attempts_provenance_insert",
    """CREATE TRIGGER execution_attempts_provenance_insert
    BEFORE INSERT ON execution_attempts
    WHEN NOT (
        EXISTS (
            SELECT 1
            FROM plan_approvals AS approval
            JOIN plan_locks AS lock
              ON lock.id = approval.plan_lock_id
            JOIN plan_versions AS plan
              ON plan.id = approval.plan_version_id
            JOIN contribution_tasks AS task
              ON task.id = approval.task_id
            JOIN analysis_versions AS analysis
              ON analysis.id = task.analysis_version_id
            JOIN opportunity_snapshots AS snapshot
              ON snapshot.id = task.snapshot_id
            JOIN contribution_task_state_versions AS approved
              ON approved.id = approval.approved_state_version_id
            JOIN contribution_task_state_versions AS executing
              ON executing.id = NEW.executing_state_version_id
            WHERE approval.id = NEW.plan_approval_id
              AND approval.task_id = NEW.task_id
              AND approval.plan_version_id = NEW.plan_version_id
              AND approval.approved_state_version_id =
                  NEW.approved_state_version_id
              AND approval.approval_hash = NEW.approval_hash
              AND approval.plan_content_hash = NEW.plan_content_hash
              AND approval.plan_record_hash = NEW.plan_record_hash
              AND approval.approved_state_record_hash =
                  NEW.approved_state_record_hash
              AND lock.plan_version_id = NEW.plan_version_id
              AND lock.task_id = NEW.task_id
              AND lock.analysis_version_id = NEW.analysis_version_id
              AND lock.snapshot_id = NEW.snapshot_id
              AND lock.base_commit_sha = NEW.base_commit_sha
              AND lock.task_record_hash = NEW.task_record_hash
              AND lock.analysis_record_hash = NEW.analysis_record_hash
              AND lock.analysis_output_hash = NEW.analysis_output_hash
              AND lock.snapshot_inputs_hash = NEW.snapshot_inputs_hash
              AND lock.provider_contract_hash = NEW.provider_contract_hash
              AND lock.plan_content_hash = NEW.plan_content_hash
              AND lock.plan_record_hash = NEW.plan_record_hash
              AND plan.task_id = NEW.task_id
              AND plan.content_hash = NEW.plan_content_hash
              AND plan.record_hash = NEW.plan_record_hash
              AND task.record_hash = NEW.task_record_hash
              AND task.analysis_version_id = NEW.analysis_version_id
              AND task.snapshot_id = NEW.snapshot_id
              AND task.analysis_record_hash = NEW.analysis_record_hash
              AND task.analysis_output_hash = NEW.analysis_output_hash
              AND task.snapshot_inputs_hash = NEW.snapshot_inputs_hash
              AND analysis.record_hash = NEW.analysis_record_hash
              AND analysis.analysis_output_hash = NEW.analysis_output_hash
              AND analysis.snapshot_inputs_hash = NEW.snapshot_inputs_hash
              AND snapshot.inputs_hash = NEW.snapshot_inputs_hash
              AND json_extract(snapshot.repository_data, '$.full_name') =
                  NEW.repository_full_name
              AND approved.task_id = NEW.task_id
              AND approved.to_state = 'plan_approved'
              AND approved.record_hash = NEW.approved_state_record_hash
              AND executing.task_id = NEW.task_id
              AND NEW.attempt_number = COALESCE(
                  (
                      SELECT MAX(existing.attempt_number) + 1
                      FROM execution_attempts AS existing
                      WHERE existing.task_id = NEW.task_id
                  ),
                  1
              )
              AND (
                  (
                      executing.sequence = approved.sequence + 1
                      AND executing.from_state = 'plan_approved'
                      AND executing.to_state = 'executing'
                      AND executing.reason_code = 'execution_started'
                      AND executing.previous_state_hash = approved.record_hash
                      AND executing.record_hash =
                          NEW.executing_state_record_hash
                      AND executing.task_record_hash = NEW.task_record_hash
                      AND NEW.attempt_number >= 1
                  )
                  OR (
                      executing.from_state = 'reviewing'
                      AND executing.to_state = 'executing'
                      AND executing.reason_code = 'repair_started'
                      AND executing.record_hash =
                          NEW.executing_state_record_hash
                      AND executing.task_record_hash = NEW.task_record_hash
                      AND NEW.attempt_number >= 2
                      AND (SELECT COUNT(*) FROM execution_attempts AS repair_budget
                          WHERE repair_budget.plan_approval_id = NEW.plan_approval_id) < 4
                      AND EXISTS (
                          SELECT 1
                          FROM review_runs AS review
                          JOIN contribution_task_state_versions AS reviewing
                            ON reviewing.id = review.reviewing_state_version_id
                          WHERE review.task_id = NEW.task_id
                            AND reviewing.to_state = 'reviewing'
                            AND executing.previous_state_hash =
                                reviewing.record_hash
                            AND executing.sequence = reviewing.sequence + 1
                      )
                  )
              )
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid execution attempt provenance');
    END""",
)


# Preserve existing demo PRs while enabling a distinct real GitHub provider.
from app.migrations.versions.v0022_publish_intents import (
    _STATEMENTS as _PUBLISH_STATEMENTS,
)

_DRAFT_CREATE = next(
    s for s in _PUBLISH_STATEMENTS if "CREATE TABLE draft_pull_requests" in s
).replace("CHECK (provider = 'fake')", "CHECK (provider IN ('fake', 'github'))")
STATEMENTS += (
    "DROP TRIGGER draft_pull_requests_no_update",
    "DROP TRIGGER draft_pull_requests_no_delete",
    "ALTER TABLE draft_pull_requests RENAME TO draft_pull_requests_legacy",
    _DRAFT_CREATE,
    "INSERT INTO draft_pull_requests SELECT * FROM draft_pull_requests_legacy",
    "DROP TABLE draft_pull_requests_legacy",
    *(
        s
        for s in _PUBLISH_STATEMENTS
        if "CREATE" in s
        and (
            "INDEX ix_draft_pull_requests_" in s
            or "TRIGGER draft_pull_requests_no_" in s
        )
    ),
)


# Identical text may need a new immutable version after the evidence/state changes.
from app.migrations.versions.v0011_plan_versions import _STATEMENTS as _PLAN_STATEMENTS
from app.migrations.versions.v0012_plan_revision_links import (
    _STATEMENTS as _REVISION_STATEMENTS,
)

_PLAN_CREATE = (
    next(s for s in _PLAN_STATEMENTS if "CREATE TABLE plan_versions" in s)
    .replace(
        "UNIQUE (task_id, content_hash)",
        "UNIQUE (task_id, parent_version_id, content_hash)",
    )
    .replace(
        "created_at DATETIME NOT NULL,",
        "created_at DATETIME NOT NULL,\n"
        "        parent_version_id VARCHAR(36) REFERENCES plan_versions(id) ON DELETE RESTRICT,\n"
        "        parent_record_hash VARCHAR(64),",
    )
)
STATEMENTS += (
    "DROP TRIGGER plan_versions_no_update",
    "DROP TRIGGER plan_versions_no_delete",
    "DROP TRIGGER plan_versions_provenance_insert",
    "DROP TRIGGER plan_versions_revision_insert",
    "ALTER TABLE plan_versions RENAME TO plan_versions_legacy",
    _PLAN_CREATE,
    "INSERT INTO plan_versions SELECT * FROM plan_versions_legacy",
    "DROP TABLE plan_versions_legacy",
    *(s for s in _PLAN_STATEMENTS if "CREATE" in s and "CREATE TABLE" not in s),
    *(s for s in _REVISION_STATEMENTS if "CREATE" in s),
)


def upgrade(connection: Connection) -> None:
    for statement in STATEMENTS:
        connection.exec_driver_sql(statement)


MIGRATION = Migration(
    revision="0029_workbench",
    description="Append-only AI contribution workbench journal",
    signature="\n".join(STATEMENTS),
    upgrade=upgrade,
    requires_foreign_keys_disabled=True,
    recovery="Stop workers, restore the pre-upgrade SQLite and artifact backup, then run the previous application. No historical plans or approvals are rewritten.",
)
