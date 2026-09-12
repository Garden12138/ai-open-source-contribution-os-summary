"""Append-only model configuration and immutable job bindings."""

from sqlalchemy import Connection
from app.migrations.core import Migration
from app.migrations.versions.v0028_nvidia_review_runs import (
    _STATEMENTS as REVIEW_STATEMENTS,
)

STATEMENTS = (
    """CREATE TABLE model_config_versions (
    id VARCHAR(36) NOT NULL PRIMARY KEY, scope VARCHAR(160) NOT NULL,
    sequence INTEGER NOT NULL, payload JSON NOT NULL,
    previous_hash VARCHAR(64), record_hash VARCHAR(64) NOT NULL UNIQUE,
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_model_config_sequence UNIQUE(scope, sequence),
    CONSTRAINT ck_model_config_sequence CHECK(sequence >= 1))""",
    "CREATE INDEX ix_model_config_versions_scope ON model_config_versions(scope)",
    """CREATE TABLE job_model_bindings (
    job_id VARCHAR(36) NOT NULL PRIMARY KEY REFERENCES jobs(id) ON DELETE RESTRICT,
    profile_id VARCHAR(36) NOT NULL REFERENCES model_config_versions(id) ON DELETE RESTRICT,
    profile_hash VARCHAR(64) NOT NULL)""",
    """CREATE TRIGGER model_config_chain BEFORE INSERT ON model_config_versions
    WHEN (NEW.sequence = 1 AND NEW.previous_hash IS NOT NULL)
    OR (NEW.sequence > 1 AND NOT EXISTS (SELECT 1 FROM model_config_versions
      WHERE scope=NEW.scope AND sequence=NEW.sequence-1 AND record_hash=NEW.previous_hash))
    BEGIN SELECT RAISE(ABORT, 'Model configuration chain mismatch'); END""",
    """CREATE TRIGGER job_model_binding_hash BEFORE INSERT ON job_model_bindings
    WHEN NOT EXISTS (SELECT 1 FROM model_config_versions WHERE id=NEW.profile_id
      AND scope LIKE 'profile:%' AND record_hash=NEW.profile_hash)
    BEGIN SELECT RAISE(ABORT, 'Model profile binding mismatch'); END""",
)


def upgrade(connection: Connection) -> None:
    for statement in STATEMENTS:
        connection.exec_driver_sql(statement)
    for table in ("model_config_versions", "job_model_bindings"):
        for operation in ("UPDATE", "DELETE"):
            connection.exec_driver_sql(
                f"CREATE TRIGGER {table}_no_{operation.lower()} BEFORE {operation} ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'Model records are immutable'); END"
            )
    for statement in REVIEW_STATEMENTS:
        statement = statement.replace(
            "'fake_blocking', 'nvidia_nim'",
            "'fake_blocking', 'nvidia_nim', 'openai_compatible'",
        )
        statement = statement.replace(
            "NEW.reviewer_kind != 'nvidia_nim'",
            "NEW.reviewer_kind NOT IN ('nvidia_nim', 'openai_compatible')",
        )
        statement = statement.replace(
            "invocation.provider_name = 'nvidia_nim'",
            "invocation.provider_name = NEW.reviewer_kind",
        )
        connection.exec_driver_sql(statement)


MIGRATION = Migration(
    revision="0030_model_settings",
    description="Versioned model settings and job bindings",
    signature="model-settings-v1:append-only-config:job-profile-hash:exact-provider-review",
    upgrade=upgrade,
    recovery="Stop all writers; restore the pre-upgrade SQLite and artifact backup together. "
    "Back up the Gateway private volume separately; never copy credentials into the business database.",
    requires_foreign_keys_disabled=True,
)
