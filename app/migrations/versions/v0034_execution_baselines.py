"""Add baseline receipt reference guards without rewriting any historical rows."""
from sqlalchemy import Connection
from app.migrations.core import Migration

STATEMENTS = (
    """CREATE TRIGGER workbench_baseline_reference BEFORE INSERT ON workbench_events
    WHEN NEW.kind = 'execution_baseline_ready' AND (
      json_extract(NEW.payload, '$.attempt_id') IS NULL
      OR NOT EXISTS (SELECT 1 FROM execution_attempts
        WHERE id = json_extract(NEW.payload, '$.attempt_id') AND task_id = NEW.task_id)
      OR length(coalesce(json_extract(NEW.payload, '$.result_hash'), '')) != 64
      OR NEW.job_id IS NULL
      OR NOT EXISTS (SELECT 1 FROM jobs WHERE id = NEW.job_id AND kind = 'execution_prepare')
    ) BEGIN SELECT RAISE(ABORT, 'Baseline attempt or job reference invalid'); END""",
)


def upgrade(connection: Connection) -> None:
    for statement in STATEMENTS:
        connection.exec_driver_sql(statement)


MIGRATION = Migration(
    revision="0034_execution_baselines", description="Bind pre-implementation baseline receipts to real attempts and preparation jobs",
    signature="\n".join(" ".join(s.split()) for s in STATEMENTS), upgrade=upgrade,
    recovery="Stop all writers and back up SQLite/artifacts before upgrade. This additive revision preserves all historical hashes and v1 dependency records. New workflow-v2 authorizations require baseline jobs and workspace dependency v2 readers. On failure restore the complete pre-upgrade backup. Never run old workers against new active execution_prepare jobs; drain/cancel jobs before restoring old code and its matching backup.",
)
