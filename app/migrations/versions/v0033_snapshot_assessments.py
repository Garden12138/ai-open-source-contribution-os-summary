"""Keep model reports intact and admit explicit, zero-model snapshot assessments."""

from sqlalchemy import Connection

from app.migrations.core import Migration
from app.migrations.versions.v0007_analysis_versions import _STATEMENTS as ORIGINAL


_ORIGIN_CHECK = """
CHECK (
    (schema_version = '1' AND job_id IS NOT NULL
     AND inspect_invocation_id IS NOT NULL AND analyze_invocation_id IS NOT NULL)
    OR
    (schema_version = '2' AND job_id IS NULL
     AND inspect_invocation_id IS NULL AND analyze_invocation_id IS NULL
     AND provider_name = 'snapshot_facts' AND adapter_version = 'snapshot-assessment-v1'
     AND model_name = 'none' AND model_version = 'none'
     AND analyze_output_schema_version = 'snapshot-assessment-v1'
     AND input_tokens = 0 AND cached_input_tokens = 0 AND output_tokens = 0
     AND estimated_cost_microusd = 0 AND duration_ms = 0)
)
"""
_CREATE = ORIGINAL[0].replace("CHECK (schema_version = '1')", _ORIGIN_CHECK)
for _column in ("job_id", "inspect_invocation_id", "analyze_invocation_id"):
    _CREATE = _CREATE.replace(f"{_column} VARCHAR(36) NOT NULL", f"{_column} VARCHAR(36)")

_TRIGGERS = tuple(
    statement.replace("WHEN NOT EXISTS", "WHEN NEW.schema_version = '1' AND NOT EXISTS", 1)
    if "CREATE TRIGGER analysis_versions_job_insert" in statement
    or "CREATE TRIGGER analysis_versions_invocations_insert" in statement
    else statement
    for statement in ORIGINAL[7:]
)
_STATEMENTS = (
    *(f"DROP TRIGGER analysis_versions_{name}" for name in (
        "provenance_insert", "job_insert", "invocations_insert", "no_update", "no_delete",
    )),
    "ALTER TABLE analysis_versions RENAME TO analysis_versions_legacy",
    _CREATE,
    "INSERT INTO analysis_versions SELECT * FROM analysis_versions_legacy",
    "DROP TABLE analysis_versions_legacy",
    *ORIGINAL[1:7],
    "CREATE UNIQUE INDEX uq_snapshot_assessment ON analysis_versions (snapshot_id) WHERE schema_version = '2'",
    *_TRIGGERS,
)


def upgrade(connection: Connection) -> None:
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0033_snapshot_assessments",
    description="Separate verified snapshot assessment from optional model reports",
    signature="\n".join(" ".join(statement.split()) for statement in _STATEMENTS),
    upgrade=upgrade,
    requires_foreign_keys_disabled=True,
    recovery=(
        "Stop all API and worker writers and back up SQLite and artifacts before upgrade. "
        "The analysis table is rebuilt, preserving every old ID, payload and hash; foreign "
        "keys are checked before commit. Schema 2 records explicitly have no model job or "
        "invocation. On failure restore the complete backup. To run older application "
        "code restore its pre-upgrade database too; never delete new task provenance "
        "or downgrade by relabelling snapshot facts as model reports."
    ),
)
