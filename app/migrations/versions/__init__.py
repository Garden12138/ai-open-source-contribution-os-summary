from app.migrations.core import Migration
from app.migrations.versions.v0001_phase1 import MIGRATION as PHASE1_BASELINE
from app.migrations.versions.v0002_provenance import MIGRATION as PROVENANCE
from app.migrations.versions.v0003_jobs import MIGRATION as JOBS
from app.migrations.versions.v0004_artifacts_audit import (
    MIGRATION as ARTIFACTS_AUDIT,
)
from app.migrations.versions.v0005_scan_selection_date import (
    MIGRATION as SCAN_SELECTION_DATE,
)
from app.migrations.versions.v0006_provider_invocations import (
    MIGRATION as PROVIDER_INVOCATIONS,
)
from app.migrations.versions.v0007_analysis_versions import (
    MIGRATION as ANALYSIS_VERSIONS,
)
from app.migrations.versions.v0008_final_score_versions import (
    MIGRATION as FINAL_SCORE_VERSIONS,
)
from app.migrations.versions.v0009_contribution_tasks import (
    MIGRATION as CONTRIBUTION_TASKS,
)
from app.migrations.versions.v0010_contribution_task_states import (
    MIGRATION as CONTRIBUTION_TASK_STATES,
)
from app.migrations.versions.v0011_plan_versions import (
    MIGRATION as PLAN_VERSIONS,
)
from app.migrations.versions.v0012_plan_revision_links import (
    MIGRATION as PLAN_REVISION_LINKS,
)
from app.migrations.versions.v0013_plan_locks import (
    MIGRATION as PLAN_LOCKS,
)
from app.migrations.versions.v0014_plan_approvals import (
    MIGRATION as PLAN_APPROVALS,
)
from app.migrations.versions.v0015_plan_conversations import (
    MIGRATION as PLAN_CONVERSATIONS,
)
from app.migrations.versions.v0016_execution_attempts import (
    MIGRATION as EXECUTION_ATTEMPTS,
)
from app.migrations.versions.v0017_execution_stage_runs import (
    MIGRATION as EXECUTION_STAGE_RUNS,
)
from app.migrations.versions.v0018_dependency_verify_inputs import (
    MIGRATION as DEPENDENCY_VERIFY_INPUTS,
)
from app.migrations.versions.v0019_execution_artifact_manifests import (
    MIGRATION as EXECUTION_ARTIFACT_MANIFESTS,
)
from app.migrations.versions.v0020_execution_workspace_disposals import (
    MIGRATION as EXECUTION_WORKSPACE_DISPOSALS,
)
from app.migrations.versions.v0021_review_runs import (
    MIGRATION as REVIEW_RUNS,
)
from app.migrations.versions.v0022_publish_intents import (
    MIGRATION as PUBLISH_INTENTS,
)
from app.migrations.versions.v0023_pull_request_events import (
    MIGRATION as PULL_REQUEST_EVENTS,
)
from app.migrations.versions.v0024_task_side_states import (
    MIGRATION as TASK_SIDE_STATES,
)
from app.migrations.versions.v0025_product_experience import (
    MIGRATION as PRODUCT_EXPERIENCE,
)
from app.migrations.versions.v0026_review_artifact_bindings import (
    MIGRATION as REVIEW_ARTIFACT_BINDINGS,
)
from app.migrations.versions.v0027_nvidia_agent_workflows import (
    MIGRATION as NVIDIA_AGENT_WORKFLOWS,
)
from app.migrations.versions.v0028_nvidia_review_runs import (
    MIGRATION as NVIDIA_REVIEW_RUNS,
)

from app.migrations.versions.v0029_workbench import MIGRATION as WORKBENCH

MIGRATIONS: tuple[Migration, ...] = (
    PHASE1_BASELINE,
    PROVENANCE,
    JOBS,
    ARTIFACTS_AUDIT,
    SCAN_SELECTION_DATE,
    PROVIDER_INVOCATIONS,
    ANALYSIS_VERSIONS,
    FINAL_SCORE_VERSIONS,
    CONTRIBUTION_TASKS,
    CONTRIBUTION_TASK_STATES,
    PLAN_VERSIONS,
    PLAN_REVISION_LINKS,
    PLAN_LOCKS,
    PLAN_APPROVALS,
    PLAN_CONVERSATIONS,
    EXECUTION_ATTEMPTS,
    EXECUTION_STAGE_RUNS,
    DEPENDENCY_VERIFY_INPUTS,
    EXECUTION_ARTIFACT_MANIFESTS,
    EXECUTION_WORKSPACE_DISPOSALS,
    REVIEW_RUNS,
    PUBLISH_INTENTS,
    PULL_REQUEST_EVENTS,
    TASK_SIDE_STATES,
    PRODUCT_EXPERIENCE,
    REVIEW_ARTIFACT_BINDINGS,
    NVIDIA_AGENT_WORKFLOWS,
    NVIDIA_REVIEW_RUNS,
    WORKBENCH,
)

__all__ = ["MIGRATIONS"]
