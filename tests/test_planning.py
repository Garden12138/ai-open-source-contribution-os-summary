from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.api import create_app
from app.artifacts import (
    ArtifactStore,
    ExecutionArtifactBundle,
    ExecutionArtifactContent,
)
from app.authorizations import UserAction
from app.approvals import (
    PLAN_APPROVAL_SCHEMA_VERSION,
    ApprovalInputFingerprint,
    PlanApprovalConflictError,
    PlanApprovalService,
    plan_approval_audit_payload,
    plan_approval_payload,
)
from app.config import Settings
from app.database import Database
from app.execution_readiness import (
    ExecutionReadinessError,
    ExecutionReadinessService,
)
from app.execution_control import (
    ExecutionControlConflictError,
    ExecutionRetryExhaustedError,
    ExecutionStageControlService,
)
from app.jobs import JobService, JobTransitionError
from app.executions import (
    ExecutionAttemptConflictError,
    ExecutionAttemptService,
    ExecutionStageStatus,
    ExecutionStageTransitionError,
)
from app.models import (
    AnalysisVersion,
    AuditEvent,
    ContributionTask,
    ContributionTaskStateVersion,
    ExecutionAttempt,
    ExecutionArtifactEntry,
    ExecutionArtifactManifest,
    ExecutionStageRun,
    ExecutionStageVersion,
    ExecutionWorkspaceDisposal,
    ExecutionWorkspaceDisposalVersion,
    Artifact,
    JobArtifact,
    PlanApproval,
    PlanConversationEntry,
    PlanLock,
    PlanVersion,
)
from app.plan_locks import (
    PLAN_LOCK_SCHEMA_VERSION,
    PlanLockConflictError,
    PlanLockService,
    plan_lock_payload,
    provider_contract_payload,
)
from app.plan_conversations import (
    PlanConversationService,
    PlanDecisionCode,
)
from app.planning import (
    CONTRIBUTION_TASK_SCHEMA_VERSION,
    ContributionTaskConflictError,
    ContributionTaskNotFoundError,
    ContributionTaskService,
)
from app.plans import (
    PLAN_SCHEMA_VERSION,
    PlanCommand,
    PlanContent,
    PlanVersionConflictError,
    PlanVersionService,
)
from app.providers import AnalysisBudget, FakeProvider, ProviderAnalysisJobWorker
from app.sandbox_worker.specs import (
    JobSpecSigner,
    SandboxCommand,
    SandboxPolicy,
)
from app.task_states import (
    LEGAL_TASK_TRANSITIONS,
    ContributionTaskState,
    ContributionTaskStateService,
    TaskStateConflictError,
    TaskStateTransitionError,
    task_state_record_payload,
)
from app.provenance import canonical_json, content_hash
from app.sandbox_worker.specs import SandboxStage
from app.sandbox_worker.implementation import DisposableWorkspace
from app.worker import DiscoveryJobWorker
from app.workspace_disposals import (
    ExecutionWorkspaceDisposalService,
    WorkspaceDisposalError,
    WorkspaceDisposalStatus,
)


NOW = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)


def _explore_artifact_bundle() -> ExecutionArtifactBundle:
    data = canonical_json(
        {
            "version": "test-explore-result-v1",
            "inventory_hash": "4" * 64,
            "file_count": 2,
        }
    ).encode("utf-8")
    result_hash = hashlib.sha256(data).hexdigest()
    return ExecutionArtifactBundle(
        stage=SandboxStage.EXPLORE,
        result_hash=result_hash,
        contents=(
            ExecutionArtifactContent.create(
                role="stage-result",
                data=data,
                media_type=(
                    "application/vnd.contribos.stage-result+json"
                ),
                expected_hash=result_hash,
            ),
        ),
    )


def _verify_artifact_bundle() -> ExecutionArtifactBundle:
    tests_data = canonical_json(
        [
            {
                "version": "normalized-test-result-v1",
                "command_id": "focused-tests",
                "outcome": "passed",
            }
        ]
    ).encode("utf-8")
    tests_hash = hashlib.sha256(tests_data).hexdigest()
    stage_data = canonical_json(
        {
            "version": "test-verify-result-v1",
            "test_results_hash": tests_hash,
            "succeeded": True,
        }
    ).encode("utf-8")
    result_hash = hashlib.sha256(stage_data).hexdigest()
    return ExecutionArtifactBundle(
        stage=SandboxStage.VERIFY,
        result_hash=result_hash,
        contents=(
            ExecutionArtifactContent.create(
                role="stage-result",
                data=stage_data,
                media_type=(
                    "application/vnd.contribos.stage-result+json"
                ),
                expected_hash=result_hash,
            ),
            ExecutionArtifactContent.create(
                role="normalized-test-results",
                data=tests_data,
                media_type=(
                    "application/vnd.contribos.test-results+json"
                ),
                expected_hash=tests_hash,
            ),
        ),
    )


class _FakeWorkspaceDestroyClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    async def destroy(self, workspace: DisposableWorkspace) -> None:
        self.calls.append(workspace.volume_name)
        if self.fail:
            raise RuntimeError("untrusted raw cleanup failure")


class _StubGitHub:
    rate_limit_remaining = 4_998
    rate_limit_reset_at = NOW + timedelta(hours=1)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def search_issues(self, query: str, limit: int):
        return [
            {
                "id": 71_001,
                "number": 41,
                "title": "Create an immutable contribution task root",
                "body": (
                    "The task must bind one frozen analysis with deterministic "
                    "acceptance evidence and persistence across restart."
                ),
                "html_url": "https://github.com/fixture/planning/issues/41",
                "repository_url": (
                    "https://api.github.com/repos/fixture/planning"
                ),
                "state": "open",
                "labels": [{"name": "help wanted"}],
                "comments": 1,
                "assignees": [],
                "author_association": "MEMBER",
                "created_at": "2026-07-01T00:00:00Z",
                "updated_at": "2026-07-29T00:00:00Z",
            }
        ]

    async def get_repository_bundle(self, full_name: str):
        return {
            "repository": {
                "id": 72_001,
                "full_name": full_name,
                "description": "Deterministic planning fixture",
                "html_url": f"https://github.com/{full_name}",
                "language": "Python",
                "license": {"spdx_id": "MIT"},
                "stargazers_count": 1_500,
                "forks_count": 50,
                "open_issues_count": 8,
                "archived": False,
                "disabled": False,
                "default_branch": "main",
                "topics": ["planning"],
                "pushed_at": "2026-07-29T00:00:00Z",
            },
            "community": {
                "health_percentage": 90,
                "files": {
                    "contributing": {
                        "url": "https://github.com/fixture/planning/CONTRIBUTING.md"
                    }
                },
            },
        }


def _seed_analysis(path: Path) -> tuple[str, str, int]:
    database_url = f"sqlite+pysqlite:///{path}"
    settings = Settings(
        database_url=database_url,
        github_queries=("offline-planning",),
    )
    app = create_app(settings)
    app.state.github_client_factory = _StubGitHub
    provider = FakeProvider()

    with TestClient(app) as client:
        discovery = client.post(
            "/api/v1/scans",
            json={},
            headers={"Idempotency-Key": "planning-discovery"},
        )
        discovery_now = (
            datetime.fromisoformat(discovery.json()["run_after"])
            + timedelta(seconds=1)
        )
        asyncio.run(
            DiscoveryJobWorker(
                app.state.database,
                app.state.settings,
                app.state.github_client_factory,
                worker_id="planning-discovery-worker",
            ).run_once(now=discovery_now)
        )
        board = client.get(
            "/api/v1/opportunities/daily",
            params={
                "on": (
                    discovery_now.replace(tzinfo=timezone.utc)
                    if discovery_now.tzinfo is None
                    else discovery_now.astimezone(timezone.utc)
                ).astimezone(
                    ZoneInfo(settings.timezone)
                ).date().isoformat()
            },
        ).json()
        pick = board["picks"][0]
        opportunity_id = pick["opportunity"]["id"]
        endpoint = (
            f"/api/v1/opportunities/{opportunity_id}/analyses"
        )
        app.state.analysis_provider = provider
        app.state.analysis_budget = AnalysisBudget()
        analysis = client.post(
            endpoint,
            json={"snapshot_id": pick["snapshot_id"]},
            headers={"Idempotency-Key": "planning-analysis"},
        )
        analysis_now = (
            datetime.fromisoformat(analysis.json()["run_after"])
            + timedelta(seconds=1)
        )
        completed = asyncio.run(
            ProviderAnalysisJobWorker(
                app.state.database,
                provider,
                worker_id="planning-provider-worker",
            ).run_once(now=analysis_now)
        )
        assert completed is not None and completed.state == "succeeded"
        history = client.get(endpoint).json()
        return (
            history["versions"][0]["id"],
            pick["snapshot_id"],
            opportunity_id,
        )


def _plan_content(*, goal: str = "Implement the accepted task safely") -> PlanContent:
    return PlanContent(
        goal=goal,
        acceptance_criteria=(
            "The immutable task source remains verifiable.",
            "Focused and regression tests pass.",
        ),
        files_to_inspect=(
            "app/planning.py",
            "tests/test_planning.py",
        ),
        files_likely_to_change=(
            "app/plans.py",
            "tests/test_planning.py",
        ),
        implementation_steps=(
            "Inspect the exact task and analysis provenance.",
            "Implement the smallest complete vertical slice.",
            "Run focused and regression verification.",
        ),
        tests_to_add_or_run=(
            "Add deterministic persistence and immutability tests.",
            "Run the full offline pytest suite.",
        ),
        commands_to_run=(
            PlanCommand(
                command_id="focused_tests",
                purpose="Verify the plan persistence boundary",
                argv=(
                    "python",
                    "-m",
                    "pytest",
                    "-q",
                    "tests/test_planning.py",
                ),
            ),
            PlanCommand(
                command_id="compile",
                purpose="Compile application Python modules",
                argv=("python", "-m", "compileall", "-q", "app"),
            ),
        ),
        risks=(
            "A stale task state must not receive a new initial plan.",
        ),
        questions_for_maintainer=(
            "Are additional platform-specific checks required?",
        ),
    )


def _plan_api_payload(
    content: PlanContent,
    *,
    parent_version_id: str | None = None,
) -> dict[str, object]:
    payload = content.hash_payload()
    payload.pop("schema_version")
    return {
        "parent_version_id": parent_version_id,
        **payload,
    }


def _start_execution_fixture(
    path: Path,
) -> tuple[str, str, str, str]:
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="execution-task",
                now=NOW,
            )
            plan = PlanVersionService(session).create_initial(
                task_id=task.id,
                content=_plan_content(),
                idempotency_key="execution-plan",
                now=NOW,
            )
            lock = PlanLockService(session).create(
                plan_version_id=plan.id,
                base_commit_sha="a" * 40,
                idempotency_key="execution-lock",
                now=NOW,
            )
            approval = PlanApprovalService(session).approve(
                plan_lock_id=lock.id,
                idempotency_key="execution-approval",
                actor_type="local_user",
                actor_id="user-1",
                action=UserAction.APPROVE_PLAN,
                now=NOW,
            )
            attempt = ExecutionAttemptService(session).start(
                approval_id=approval.id,
                observed=ApprovalInputFingerprint.from_lock(lock),
                action=UserAction.START_EXECUTION,
                idempotency_key="execution-start",
                actor_type="local_user",
                actor_id="user-1",
                repository_archive_hash="b" * 64,
                runner_image_digest="sha256:" + "c" * 64,
                sandbox_policy=SandboxPolicy(),
                now=NOW,
            )
            return (
                attempt.id,
                task.id,
                approval.id,
                attempt.record_hash,
            )
    finally:
        database.close()


def test_contribution_task_is_idempotent_hash_bound_and_restart_safe(
    tmp_path: Path,
) -> None:
    path = tmp_path / "contribution-task.db"
    analysis_id, snapshot_id, opportunity_id = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            service = ContributionTaskService(session)
            task = service.create(
                analysis_version_id=analysis_id,
                idempotency_key="task-create-1",
                now=NOW,
            )
            replay = service.create(
                analysis_version_id=analysis_id,
                idempotency_key="task-create-1",
                now=NOW + timedelta(minutes=1),
            )
            natural_replay = service.create(
                analysis_version_id=analysis_id,
                idempotency_key="task-create-new-request",
                now=NOW + timedelta(minutes=2),
            )

            assert task.id == replay.id == natural_replay.id
            assert task.schema_version == CONTRIBUTION_TASK_SCHEMA_VERSION
            assert task.analysis_version_id == analysis_id
            assert task.snapshot_id == snapshot_id
            assert task.opportunity_id == opportunity_id
            assert task.idempotency_key == "task-create-1"
            assert task.analysis_record_hash
            assert task.analysis_output_hash
            assert task.snapshot_inputs_hash
            assert len(task.record_hash) == 64
            assert session.scalar(
                select(func.count()).select_from(ContributionTask)
            ) == 1
            current = ContributionTaskStateService(session).current(task.id)
            assert current.sequence == 1
            assert current.from_state is None
            assert current.to_state == ContributionTaskState.PLANNING.value
            assert current.reason_code == "created_from_analysis"
            assert current.task_record_hash == task.record_hash
            assert current.previous_state_hash is None
            assert current.record_hash == content_hash(
                task_state_record_payload(
                    task_id=task.id,
                    task_record_hash=task.record_hash,
                    sequence=1,
                    from_state=None,
                    to_state=ContributionTaskState.PLANNING,
                    reason_code="created_from_analysis",
                    previous_state_hash=None,
                )
            )
    finally:
        database.close()

    restarted = Database(f"sqlite+pysqlite:///{path}")
    restarted.create_schema()
    try:
        with restarted.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="task-create-after-restart",
                now=NOW + timedelta(days=1),
            )
            assert task.analysis_version_id == analysis_id
            assert task.idempotency_key == "task-create-1"
            assert session.scalar(
                select(func.count()).select_from(ContributionTask)
            ) == 1
            current = ContributionTaskStateService(session).current(task.id)
            assert current.sequence == 1
            assert current.to_state == "planning"
    finally:
        restarted.close()


def test_create_contribution_task_api_is_protected_and_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "contribution-task-api.db"
    analysis_id, snapshot_id, opportunity_id = _seed_analysis(path)
    app = create_app(
        Settings(database_url=f"sqlite+pysqlite:///{path}")
    )
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/tasks",
            json={"analysis_version_id": analysis_id},
            headers={"Idempotency-Key": "task-api-create"},
        )
        replay = client.post(
            "/api/v1/tasks",
            json={"analysis_version_id": analysis_id},
            headers={"Idempotency-Key": "task-api-create"},
        )
        natural_replay = client.post(
            "/api/v1/tasks",
            json={"analysis_version_id": analysis_id},
            headers={"Idempotency-Key": "task-api-new-request"},
        )
        missing = client.post(
            "/api/v1/tasks",
            json={"analysis_version_id": "missing-analysis"},
            headers={"Idempotency-Key": "task-api-missing"},
        )
        invalid_key = client.post(
            "/api/v1/tasks",
            json={"analysis_version_id": analysis_id},
            headers={"Idempotency-Key": "bad key"},
        )

    assert created.status_code == 201
    assert replay.status_code == 201
    assert natural_replay.status_code == 201
    assert created.json() == replay.json() == natural_replay.json()
    assert created.json()["analysis_version_id"] == analysis_id
    assert created.json()["snapshot_id"] == snapshot_id
    assert created.json()["opportunity_id"] == opportunity_id
    assert len(created.json()["record_hash"]) == 64
    assert "idempotency_key" not in created.json()
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "http_404"
    assert invalid_key.status_code == 422
    assert invalid_key.json()["error"]["code"] == "http_422"


def test_create_plan_version_api_supports_initial_revision_and_conflicts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plan-version-api.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="plan-api-task",
            )
            task_id = task.id
    finally:
        database.close()
    app = create_app(
        Settings(database_url=f"sqlite+pysqlite:///{path}")
    )
    first_content = _plan_content()
    with TestClient(app) as client:
        initial = client.post(
            f"/api/v1/tasks/{task_id}/plan-versions",
            json=_plan_api_payload(first_content),
            headers={"Idempotency-Key": "plan-api-initial"},
        )
        replay = client.post(
            f"/api/v1/tasks/{task_id}/plan-versions",
            json=_plan_api_payload(first_content),
            headers={"Idempotency-Key": "plan-api-initial"},
        )
        second_content = replace(
            first_content,
            goal="Create the revised API plan safely",
        )
        revision = client.post(
            f"/api/v1/tasks/{task_id}/plan-versions",
            json=_plan_api_payload(
                second_content,
                parent_version_id=initial.json()["id"],
            ),
            headers={"Idempotency-Key": "plan-api-revision"},
        )
        wrong_task = client.post(
            "/api/v1/tasks/different-task/plan-versions",
            json=_plan_api_payload(
                replace(
                    second_content,
                    goal="A cross-task revision must fail",
                ),
                parent_version_id=revision.json()["id"],
            ),
            headers={"Idempotency-Key": "plan-api-cross-task"},
        )
        unsafe_payload = _plan_api_payload(
            replace(
                second_content,
                goal="Unsafe paths must fail",
            ),
            parent_version_id=revision.json()["id"],
        )
        unsafe_payload["files_to_inspect"] = ["../host-secret"]
        unsafe = client.post(
            f"/api/v1/tasks/{task_id}/plan-versions",
            json=unsafe_payload,
            headers={"Idempotency-Key": "plan-api-unsafe"},
        )
        missing = client.post(
            "/api/v1/tasks/missing-task/plan-versions",
            json=_plan_api_payload(first_content),
            headers={"Idempotency-Key": "plan-api-missing"},
        )

    assert initial.status_code == 201
    assert replay.status_code == 201
    assert initial.json() == replay.json()
    assert initial.json()["task_id"] == task_id
    assert initial.json()["version_number"] == 1
    assert initial.json()["parent_version_id"] is None
    assert initial.json()["commands_to_run"][0]["argv"][0] == "python"
    assert revision.status_code == 201
    assert revision.json()["version_number"] == 2
    assert revision.json()["parent_version_id"] == initial.json()["id"]
    assert revision.json()["parent_record_hash"] == initial.json()[
        "record_hash"
    ]
    assert wrong_task.status_code == 409
    assert unsafe.status_code == 422
    assert missing.status_code == 404


def test_approve_plan_version_api_locks_audits_and_is_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plan-approval-api.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="approval-api-task",
            )
            plan = PlanVersionService(session).create_initial(
                task_id=task.id,
                content=_plan_content(),
                idempotency_key="approval-api-plan",
            )
            task_id = task.id
            plan_id = plan.id
    finally:
        database.close()
    app = create_app(
        Settings(database_url=f"sqlite+pysqlite:///{path}")
    )
    payload = {
        "base_commit_sha": "b" * 40,
        "actor_id": "user-1",
    }
    with TestClient(app) as client:
        approved = client.post(
            f"/api/v1/plan-versions/{plan_id}/approve",
            json=payload,
            headers={"Idempotency-Key": "approval-api-create"},
        )
        replay = client.post(
            f"/api/v1/plan-versions/{plan_id}/approve",
            json=payload,
            headers={"Idempotency-Key": "approval-api-create"},
        )
        natural_replay = client.post(
            f"/api/v1/plan-versions/{plan_id}/approve",
            json=payload,
            headers={"Idempotency-Key": "approval-api-new-request"},
        )
        different_base = client.post(
            f"/api/v1/plan-versions/{plan_id}/approve",
            json={
                **payload,
                "base_commit_sha": "c" * 40,
            },
            headers={"Idempotency-Key": "approval-api-different-base"},
        )
        different_actor = client.post(
            f"/api/v1/plan-versions/{plan_id}/approve",
            json={
                **payload,
                "actor_id": "user-2",
            },
            headers={"Idempotency-Key": "approval-api-different-actor"},
        )
        invalid_sha = client.post(
            f"/api/v1/plan-versions/{plan_id}/approve",
            json={
                **payload,
                "base_commit_sha": "not-a-sha",
            },
            headers={"Idempotency-Key": "approval-api-invalid-sha"},
        )
        missing = client.post(
            "/api/v1/plan-versions/missing-plan/approve",
            json=payload,
            headers={"Idempotency-Key": "approval-api-missing"},
        )

    assert approved.status_code == 201
    assert replay.status_code == 201
    assert natural_replay.status_code == 201
    assert approved.json() == replay.json() == natural_replay.json()
    assert approved.json()["plan_version_id"] == plan_id
    assert approved.json()["task_id"] == task_id
    assert approved.json()["actor_type"] == "local_user"
    assert approved.json()["actor_id"] == "user-1"
    assert len(approved.json()["approval_hash"]) == 64
    assert different_base.status_code == 409
    assert different_actor.status_code == 409
    assert invalid_sha.status_code == 422
    assert invalid_sha.json()["error"]["code"] == "validation_error"
    assert missing.status_code == 404

    check = Database(f"sqlite+pysqlite:///{path}")
    check.create_schema()
    try:
        with check.session() as session:
            assert ContributionTaskStateService(
                session
            ).current(task_id).to_state == "plan_approved"
            assert session.scalar(
                select(func.count()).select_from(PlanApproval)
            ) == 1
            assert session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "plan.approved")
            ) == 1
    finally:
        check.close()


def test_create_execution_api_is_exact_atomic_and_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution-create-api.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="execution-api-task",
            )
            first = PlanVersionService(session).create_initial(
                task_id=task.id,
                content=_plan_content(),
                idempotency_key="execution-api-plan-1",
            )
            current = PlanVersionService(session).create_revision(
                parent_version_id=first.id,
                content=replace(
                    _plan_content(),
                    goal="Execute only the exact current approved plan",
                ),
                idempotency_key="execution-api-plan-2",
            )
            task_id = task.id
            first_plan_id = first.id
            plan_id = current.id
    finally:
        database.close()

    base_sha = "a" * 40
    archive_hash = "b" * 64
    runner_digest = "sha256:" + "c" * 64
    app = create_app(
        Settings(database_url=f"sqlite+pysqlite:///{path}")
    )
    with TestClient(app) as client:
        approval = client.post(
            f"/api/v1/plan-versions/{plan_id}/approve",
            json={
                "base_commit_sha": base_sha,
                "actor_id": "user-1",
            },
            headers={"Idempotency-Key": "execution-api-approval"},
        )
        assert approval.status_code == 201
        payload = {
            "approval_id": approval.json()["id"],
            "base_commit_sha": base_sha,
            "repository_archive_hash": archive_hash,
            "runner_image_digest": runner_digest,
            "actor_id": "user-1",
        }
        wrong_route_plan = client.post(
            f"/api/v1/plan-versions/{first_plan_id}/executions",
            json=payload,
            headers={"Idempotency-Key": "execution-api-wrong-plan"},
        )
        missing_plan = client.post(
            "/api/v1/plan-versions/missing-plan/executions",
            json=payload,
            headers={"Idempotency-Key": "execution-api-missing-plan"},
        )
        missing_approval = client.post(
            f"/api/v1/plan-versions/{plan_id}/executions",
            json={
                **payload,
                "approval_id": "missing-approval",
            },
            headers={"Idempotency-Key": "execution-api-missing-approval"},
        )
        invalid_digest = client.post(
            f"/api/v1/plan-versions/{plan_id}/executions",
            json={
                **payload,
                "runner_image_digest": "latest",
            },
            headers={"Idempotency-Key": "execution-api-invalid-digest"},
        )
        secret_actor = "ghp_executionsecretcanary1234567890"
        credential_rejected = client.post(
            f"/api/v1/plan-versions/{plan_id}/executions",
            json={
                **payload,
                "actor_id": secret_actor,
            },
            headers={"Idempotency-Key": "execution-api-secret-actor"},
        )
        created = client.post(
            f"/api/v1/plan-versions/{plan_id}/executions",
            json=payload,
            headers={"Idempotency-Key": "execution-api-create"},
        )
        replay = client.post(
            f"/api/v1/plan-versions/{plan_id}/executions",
            json=payload,
            headers={"Idempotency-Key": "execution-api-create"},
        )
        changed_replay = client.post(
            f"/api/v1/plan-versions/{plan_id}/executions",
            json={
                **payload,
                "repository_archive_hash": "d" * 64,
            },
            headers={"Idempotency-Key": "execution-api-create"},
        )
        second_start = client.post(
            f"/api/v1/plan-versions/{plan_id}/executions",
            json=payload,
            headers={"Idempotency-Key": "execution-api-second-start"},
        )
        readback = client.get(
            f"/api/v1/executions/{created.json()['id']}"
        )
        empty_artifacts = client.get(
            f"/api/v1/executions/{created.json()['id']}/artifacts"
        )
        executing_task = client.get(f"/api/v1/tasks/{task_id}")

    assert wrong_route_plan.status_code == 409
    assert missing_plan.status_code == 404
    assert missing_approval.status_code == 404
    assert invalid_digest.status_code == 422
    assert invalid_digest.json()["error"]["code"] == "validation_error"
    assert credential_rejected.status_code == 422
    assert secret_actor not in credential_rejected.text
    assert created.status_code == 201
    assert replay.status_code == 201
    assert created.json() == replay.json()
    body = created.json()
    assert body["task_id"] == task_id
    assert body["plan_version_id"] == plan_id
    assert body["plan_approval_id"] == approval.json()["id"]
    assert body["attempt_number"] == 1
    assert body["action"] == UserAction.START_EXECUTION.value
    assert body["actor_type"] == "local_user"
    assert body["actor_id"] == "user-1"
    assert body["repository_full_name"] == "fixture/planning"
    assert body["base_commit_sha"] == base_sha
    assert body["repository_archive_hash"] == archive_hash
    assert body["runner_image_digest"] == runner_digest
    assert body["sandbox_policy_version"] == SandboxPolicy().version
    assert body["sandbox_policy_hash"] == SandboxPolicy().policy_hash
    assert body["current_stage"]["sequence"] == 1
    assert body["current_stage"]["stage"] == "explore"
    assert body["current_stage"]["status"] == "pending"
    assert body["current_stage"]["reason_code"] == "execution_started"
    assert body["current_stage"]["attempt_record_hash"] == body["record_hash"]
    assert "idempotency_key" not in body
    assert "workspace_ref" not in body["current_stage"]
    assert changed_replay.status_code == 409
    assert second_start.status_code == 409
    assert readback.status_code == 200
    assert readback.json()["stages"] == [body["current_stage"]]
    assert readback.json()["stage_runs"] == []
    assert readback.json()["artifact_manifests"] == []
    assert empty_artifacts.status_code == 200
    assert empty_artifacts.json()["manifests"] == []
    assert executing_task.status_code == 200
    assert executing_task.json()["approval_status"] == "approved"
    assert executing_task.json()["active_approval_id"] == approval.json()["id"]
    assert executing_task.json()["execution_attempt_ids"] == [body["id"]]
    assert executing_task.json()["latest_execution_attempt_id"] == body["id"]

    check = Database(f"sqlite+pysqlite:///{path}")
    check.create_schema()
    try:
        with check.session() as session:
            assert ContributionTaskStateService(
                session
            ).current(task_id).to_state == "executing"
            assert session.scalar(
                select(func.count()).select_from(ExecutionAttempt)
            ) == 1
            assert session.scalar(
                select(func.count()).select_from(ExecutionStageVersion)
            ) == 1
            assert session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "execution.started")
            ) == 1
    finally:
        check.close()


def test_create_execution_api_revokes_stale_approval_without_starting(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution-create-stale-api.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="execution-stale-api-task",
            )
            plan = PlanVersionService(session).create_initial(
                task_id=task.id,
                content=_plan_content(),
                idempotency_key="execution-stale-api-plan",
            )
            task_id = task.id
            plan_id = plan.id
    finally:
        database.close()

    app = create_app(
        Settings(database_url=f"sqlite+pysqlite:///{path}")
    )
    with TestClient(app) as client:
        approval = client.post(
            f"/api/v1/plan-versions/{plan_id}/approve",
            json={
                "base_commit_sha": "a" * 40,
                "actor_id": "user-1",
            },
            headers={"Idempotency-Key": "execution-stale-api-approval"},
        )
        assert approval.status_code == 201
        stale = client.post(
            f"/api/v1/plan-versions/{plan_id}/executions",
            json={
                "approval_id": approval.json()["id"],
                "base_commit_sha": "e" * 40,
                "repository_archive_hash": "b" * 64,
                "runner_image_digest": "sha256:" + "c" * 64,
                "actor_id": "user-1",
            },
            headers={"Idempotency-Key": "execution-stale-api-create"},
        )

    assert stale.status_code == 409
    assert "base_commit_changed" in stale.text
    check = Database(f"sqlite+pysqlite:///{path}")
    check.create_schema()
    try:
        with check.session() as session:
            assert ContributionTaskStateService(
                session
            ).current(task_id).to_state == "planning"
            assert session.scalar(
                select(func.count()).select_from(ExecutionAttempt)
            ) == 0
            assert session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "execution.started")
            ) == 0
    finally:
        check.close()


def test_execution_read_apis_verify_provenance_ownership_and_content(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution-read-api.db"
    artifact_root = tmp_path / "execution-read-artifacts"
    attempt_id, _, _, _ = _start_execution_fixture(path)
    policy = SandboxPolicy()
    signer = JobSpecSigner(
        key_id="execution-read-api-key",
        signing_key=b"r" * 32,
    )
    bundle = _explore_artifact_bundle()
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            attempts = ExecutionAttemptService(session)
            control = ExecutionStageControlService(session)
            signed = signer.sign(
                attempts.build_current_job_spec(attempt_id)
            )
            run = control.schedule_current(
                attempt_id,
                signed_job_spec=signed,
                signer=signer,
                sandbox_policy=policy,
                idempotency_key="execution-read-api-run",
                now=NOW + timedelta(minutes=1),
            )
            control.lease_next(
                worker_id="execution-read-api-worker",
                lease_seconds=30,
                now=NOW + timedelta(minutes=1),
            )
            control.begin(
                run.id,
                worker_id="execution-read-api-worker",
                signed_job_spec=signed,
                signer=signer,
                sandbox_policy=policy,
                now=NOW + timedelta(minutes=1, seconds=1),
            )
            store = ArtifactStore(session, artifact_root)
            control.complete(
                run.id,
                worker_id="execution-read-api-worker",
                status=ExecutionStageStatus.SUCCEEDED,
                reason_code="explore_succeeded",
                result_hash=bundle.result_hash,
                artifact_store=store,
                artifact_bundle=bundle,
                now=NOW + timedelta(minutes=1, seconds=2),
            )
            unrelated = store.store_bytes(
                b'{"unrelated":true}',
                media_type="application/json",
                now=NOW + timedelta(minutes=2),
            )
            unrelated_id = unrelated.id
    finally:
        database.close()

    app = create_app(
        Settings(
            database_url=f"sqlite+pysqlite:///{path}",
            artifact_root=str(artifact_root),
        )
    )
    with TestClient(app) as client:
        detail = client.get(f"/api/v1/executions/{attempt_id}")
        listing = client.get(
            f"/api/v1/executions/{attempt_id}/artifacts"
        )
        content = client.get(
            f"/api/v1/executions/{attempt_id}"
            f"/artifacts/{bundle.result_hash}"
        )
        cached = client.get(
            f"/api/v1/executions/{attempt_id}"
            f"/artifacts/{bundle.result_hash}",
            headers={"If-None-Match": content.headers["etag"]},
        )
        unrelated_content = client.get(
            f"/api/v1/executions/{attempt_id}"
            f"/artifacts/{unrelated_id}"
        )
        missing_artifact = client.get(
            f"/api/v1/executions/{attempt_id}/artifacts/{'f' * 64}"
        )
        missing_execution = client.get(
            "/api/v1/executions/missing-execution"
        )

    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["id"] == attempt_id
    assert [item["status"] for item in detail_body["stages"]] == [
        "pending",
        "running",
        "succeeded",
    ]
    assert detail_body["current_stage"]["status"] == "succeeded"
    assert len(detail_body["stage_runs"]) == 1
    assert detail_body["stage_runs"][0]["job"]["state"] == "succeeded"
    assert "idempotency_key" not in detail_body["stage_runs"][0]
    assert "lease_owner" not in detail_body["stage_runs"][0]["job"]
    assert len(detail_body["artifact_manifests"]) == 1
    manifest = detail_body["artifact_manifests"][0]
    assert manifest["stage"] == "explore"
    assert manifest["result_hash"] == bundle.result_hash
    assert manifest["entry_count"] == 1
    assert manifest["entries"][0]["role"] == "stage-result"
    assert manifest["entries"][0]["artifact_id"] == bundle.result_hash
    assert manifest["entries"][0]["content_url"] == (
        f"/api/v1/executions/{attempt_id}/artifacts/{bundle.result_hash}"
    )
    assert listing.status_code == 200
    assert listing.json()["execution_attempt_id"] == attempt_id
    assert listing.json()["manifests"] == detail_body[
        "artifact_manifests"
    ]
    assert content.status_code == 200
    assert content.content == bundle.contents[0].data
    assert content.headers["content-type"].startswith(
        "application/vnd.contribos.stage-result+json"
    )
    assert content.headers["etag"] == (
        f'"sha256:{bundle.result_hash}"'
    )
    assert content.headers["x-content-type-options"] == "nosniff"
    assert content.headers["x-contribos-artifact-role"] == "stage-result"
    assert "attachment;" in content.headers["content-disposition"]
    assert cached.status_code == 304
    assert cached.content == b""
    assert unrelated_content.status_code == 404
    assert missing_artifact.status_code == 404
    assert missing_execution.status_code == 404

    artifact_path = artifact_root / (
        f"sha256/{bundle.result_hash[:2]}/{bundle.result_hash}"
    )
    artifact_path.write_bytes(b"tampered execution artifact")
    with TestClient(app) as client:
        tampered_content = client.get(
            f"/api/v1/executions/{attempt_id}"
            f"/artifacts/{bundle.result_hash}"
        )
        tampered_listing = client.get(
            f"/api/v1/executions/{attempt_id}/artifacts"
        )
    assert tampered_content.status_code == 409
    assert tampered_listing.status_code == 409
    assert b"tampered execution artifact" not in tampered_content.content


def test_contribution_task_detail_api_returns_verified_aggregate_and_status(
    tmp_path: Path,
) -> None:
    path = tmp_path / "contribution-task-detail-api.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="detail-api-task",
            )
            plan = PlanVersionService(session).create_initial(
                task_id=task.id,
                content=_plan_content(),
                idempotency_key="detail-api-plan",
            )
            PlanConversationService(session).append_message(
                task_id=task.id,
                plan_version_id=plan.id,
                actor_type="local_user",
                actor_id="user-1",
                text="Show this persisted planning context.",
                idempotency_key="detail-api-message",
            )
            lock = PlanLockService(session).create(
                plan_version_id=plan.id,
                base_commit_sha="d" * 40,
                idempotency_key="detail-api-lock",
            )
            approval = PlanApprovalService(session).approve(
                plan_lock_id=lock.id,
                idempotency_key="detail-api-approval",
                actor_type="local_user",
                actor_id="user-1",
                action=UserAction.APPROVE_PLAN,
            )
            task_id = task.id
            plan_id = plan.id
            lock_id = lock.id
            approval_id = approval.id
    finally:
        database.close()
    app = create_app(
        Settings(database_url=f"sqlite+pysqlite:///{path}")
    )
    with TestClient(app) as client:
        detail = client.get(f"/api/v1/tasks/{task_id}")
        missing = client.get("/api/v1/tasks/missing-task")

    assert detail.status_code == 200
    body = detail.json()
    assert body["task"]["id"] == task_id
    assert body["current_state"]["to_state"] == "plan_approved"
    assert body["latest_plan_version_id"] == plan_id
    assert body["active_approval_id"] == approval_id
    assert body["approval_status"] == "approved"
    assert [item["id"] for item in body["plan_versions"]] == [plan_id]
    assert [item["id"] for item in body["plan_locks"]] == [lock_id]
    assert [item["id"] for item in body["approvals"]] == [approval_id]
    assert body["conversation"][0]["content"]["text"] == (
        "Show this persisted planning context."
    )
    assert body["plan_locks"][0]["base_commit_sha"] == "d" * 40
    assert missing.status_code == 404

    changed = Database(f"sqlite+pysqlite:///{path}")
    changed.create_schema()
    try:
        with changed.session() as session:
            approval = PlanApprovalService(session).get_verified(approval_id)
            current = ContributionTaskStateService(session).current(task_id)
            lock = PlanLockService(session).get_verified(lock_id)
            PlanApprovalService(session).revoke_if_stale(
                approval.id,
                observed=replace(
                    ApprovalInputFingerprint.from_lock(lock),
                    base_commit_sha="e" * 40,
                ),
                expected_sequence=current.sequence,
                expected_state_record_hash=current.record_hash,
            )
    finally:
        changed.close()
    with TestClient(
        create_app(Settings(database_url=f"sqlite+pysqlite:///{path}"))
    ) as client:
        revoked = client.get(f"/api/v1/tasks/{task_id}")
    assert revoked.status_code == 200
    assert revoked.json()["current_state"]["to_state"] == "planning"
    assert revoked.json()["active_approval_id"] is None
    assert revoked.json()["approval_status"] == "revoked"


def test_planning_ui_support_apis_cover_chat_diff_and_readiness(
    tmp_path: Path,
) -> None:
    path = tmp_path / "planning-ui-support-api.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="ui-support-task",
            )
            first = PlanVersionService(session).create_initial(
                task_id=task.id,
                content=_plan_content(),
                idempotency_key="ui-support-plan-1",
            )
            second = PlanVersionService(session).create_revision(
                parent_version_id=first.id,
                content=replace(
                    _plan_content(),
                    goal="Use the revised UI support plan",
                ),
                idempotency_key="ui-support-plan-2",
            )
            task_id = task.id
            first_id = first.id
            second_id = second.id
    finally:
        database.close()
    app = create_app(
        Settings(database_url=f"sqlite+pysqlite:///{path}")
    )
    base_sha = "f" * 40
    with TestClient(app) as client:
        message = client.post(
            f"/api/v1/tasks/{task_id}/conversation/messages",
            json={
                "plan_version_id": second_id,
                "actor_id": "local-user",
                "text": "Review the revised plan before approval.",
            },
            headers={"Idempotency-Key": "ui-support-message"},
        )
        comparison = client.get(
            f"/api/v1/tasks/{task_id}/plan-versions/compare",
            params={
                "left_version_id": first_id,
                "right_version_id": second_id,
            },
        )
        approval = client.post(
            f"/api/v1/plan-versions/{second_id}/approve",
            json={
                "base_commit_sha": base_sha,
                "actor_id": "local-user",
            },
            headers={"Idempotency-Key": "ui-support-approval"},
        )
        ready = client.post(
            "/api/v1/plan-approvals/"
            f"{approval.json()['id']}/execution-readiness",
            json={"base_commit_sha": base_sha},
        )
        stale = client.post(
            "/api/v1/plan-approvals/"
            f"{approval.json()['id']}/execution-readiness",
            json={"base_commit_sha": "e" * 40},
        )
        detail = client.get(f"/api/v1/tasks/{task_id}")

    assert message.status_code == 201
    assert message.json()["sequence"] == 1
    assert message.json()["entry_type"] == "message"
    assert comparison.status_code == 200
    assert comparison.json()["task_id"] == task_id
    assert comparison.json()["semantic_differences"][0]["path"] == "/goal"
    assert "--- plan-v1" in comparison.json()["unified_diff"]
    assert approval.status_code == 201
    assert ready.status_code == 200
    assert ready.json()["ready"] is True
    assert ready.json()["execution_started"] is False
    assert stale.status_code == 409
    assert "base_commit_changed" in stale.text
    assert detail.status_code == 200
    assert detail.json()["approval_status"] == "revoked"


def test_contribution_task_rejects_missing_unsafe_or_tampered_analysis(
    tmp_path: Path,
) -> None:
    path = tmp_path / "contribution-task-invalid.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            service = ContributionTaskService(session)
            with pytest.raises(ContributionTaskNotFoundError):
                service.create(
                    analysis_version_id="missing-analysis",
                    idempotency_key="task-missing",
                )
            with pytest.raises(ValueError, match="idempotency"):
                service.create(
                    analysis_version_id=analysis_id,
                    idempotency_key="ghp_tasksecretcanary1234567890",
                )

        with database.engine.begin() as connection:
            connection.execute(
                text("DROP TRIGGER analysis_versions_no_update")
            )
            connection.execute(
                text(
                    "UPDATE analysis_versions "
                    "SET record_hash = :record_hash WHERE id = :id"
                ),
                {
                    "id": analysis_id,
                    "record_hash": "0" * 64,
                },
            )

        with database.session() as session:
            with pytest.raises(
                ContributionTaskConflictError,
                match="record hash",
            ):
                ContributionTaskService(session).create(
                    analysis_version_id=analysis_id,
                    idempotency_key="task-tampered-analysis",
                )
            assert session.scalar(
                select(func.count()).select_from(ContributionTask)
            ) == 0
    finally:
        database.close()


def test_initial_plan_version_is_structured_idempotent_and_restart_safe(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plan-version.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    content = _plan_content()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="plan-task-root",
                now=NOW,
            )
            plan = PlanVersionService(session).create_initial(
                task_id=task.id,
                content=content,
                idempotency_key="plan-initial-1",
                now=NOW + timedelta(minutes=1),
            )
            replay = PlanVersionService(session).create_initial(
                task_id=task.id,
                content=content,
                idempotency_key="plan-initial-1",
                now=NOW + timedelta(minutes=2),
            )
            natural_replay = PlanVersionService(session).create_initial(
                task_id=task.id,
                content=content,
                idempotency_key="plan-initial-new-request",
                now=NOW + timedelta(minutes=3),
            )
            state = ContributionTaskStateService(session).current(task.id)

            assert plan.id == replay.id == natural_replay.id
            assert plan.schema_version == PLAN_SCHEMA_VERSION
            assert plan.version_number == 1
            assert plan.task_id == task.id
            assert plan.task_record_hash == task.record_hash
            assert plan.task_state_version_id == state.id
            assert plan.task_state_record_hash == state.record_hash
            assert plan.goal == content.goal
            assert plan.acceptance_criteria == list(
                content.acceptance_criteria
            )
            assert plan.files_to_inspect == list(content.files_to_inspect)
            assert plan.files_likely_to_change == list(
                content.files_likely_to_change
            )
            assert plan.implementation_steps == list(
                content.implementation_steps
            )
            assert plan.tests_to_add_or_run == list(
                content.tests_to_add_or_run
            )
            assert plan.commands_to_run == [
                command.hash_payload()
                for command in content.commands_to_run
            ]
            assert plan.risks == list(content.risks)
            assert plan.questions_for_maintainer == list(
                content.questions_for_maintainer
            )
            assert plan.content_hash == content.content_hash
            assert plan.record_hash == content_hash(
                {
                    "schema_version": PLAN_SCHEMA_VERSION,
                    "task_id": task.id,
                    "task_record_hash": task.record_hash,
                    "task_state_version_id": state.id,
                    "task_state_record_hash": state.record_hash,
                    "version_number": 1,
                    "content_hash": content.content_hash,
                }
            )
            assert session.scalar(
                select(func.count()).select_from(PlanVersion)
            ) == 1
            task_id = task.id
            plan_id = plan.id
    finally:
        database.close()

    restarted = Database(f"sqlite+pysqlite:///{path}")
    restarted.create_schema()
    try:
        with restarted.session() as session:
            plan = PlanVersionService(session).create_initial(
                task_id=task_id,
                content=content,
                idempotency_key="plan-initial-after-restart",
            )
            assert plan.id == plan_id
            assert plan.content_hash == content.content_hash
            assert session.scalar(
                select(func.count()).select_from(PlanVersion)
            ) == 1
    finally:
        restarted.close()


def test_plan_revisions_link_latest_parent_and_produce_stable_differences(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plan-revisions.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    first_content = _plan_content()
    second_content = replace(
        first_content,
        goal="Implement and document the accepted task safely",
        risks=(
            *first_content.risks,
            "The migration must preserve existing plan rows.",
        ),
    )
    third_content = replace(
        second_content,
        goal="A stale branch must not be accepted",
    )
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="revision-task",
                now=NOW,
            )
            plans = PlanVersionService(session)
            first = plans.create_initial(
                task_id=task.id,
                content=first_content,
                idempotency_key="revision-plan-v1",
                now=NOW,
            )
            second = plans.create_revision(
                parent_version_id=first.id,
                content=second_content,
                idempotency_key="revision-plan-v2",
                now=NOW + timedelta(minutes=1),
            )
            exact_replay = plans.create_revision(
                parent_version_id=first.id,
                content=second_content,
                idempotency_key="revision-plan-v2",
                now=NOW + timedelta(minutes=2),
            )
            natural_replay = plans.create_revision(
                parent_version_id=first.id,
                content=second_content,
                idempotency_key="revision-plan-v2-new-request",
                now=NOW + timedelta(minutes=3),
            )

            assert second.id == exact_replay.id == natural_replay.id
            assert first.parent_version_id is None
            assert first.parent_record_hash is None
            assert second.version_number == 2
            assert second.parent_version_id == first.id
            assert second.parent_record_hash == first.record_hash
            assert second.record_hash == content_hash(
                {
                    "schema_version": PLAN_SCHEMA_VERSION,
                    "task_id": task.id,
                    "task_record_hash": task.record_hash,
                    "task_state_version_id": second.task_state_version_id,
                    "task_state_record_hash": second.task_state_record_hash,
                    "version_number": 2,
                    "content_hash": second_content.content_hash,
                    "parent_version_id": first.id,
                    "parent_record_hash": first.record_hash,
                }
            )
            comparison = plans.compare(first.id, second.id)
            difference_paths = {
                item.path for item in comparison.semantic_differences
            }
            assert comparison.task_id == task.id
            assert "/goal" in difference_paths
            assert "/risks/1" in difference_paths
            assert first_content.goal in comparison.unified_diff
            assert second_content.goal in comparison.unified_diff
            assert "--- plan-v1" in comparison.unified_diff
            assert "+++ plan-v2" in comparison.unified_diff

            with pytest.raises(
                PlanVersionConflictError,
                match="unchanged",
            ):
                plans.create_revision(
                    parent_version_id=second.id,
                    content=second_content,
                    idempotency_key="revision-plan-unchanged",
                )
            with pytest.raises(
                PlanVersionConflictError,
                match="stale",
            ):
                plans.create_revision(
                    parent_version_id=first.id,
                    content=third_content,
                    idempotency_key="revision-plan-stale-parent",
                )
            assert session.scalar(
                select(func.count()).select_from(PlanVersion)
            ) == 2
    finally:
        database.close()


def test_plan_lock_binds_exact_inputs_without_approving_and_survives_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plan-lock.db"
    analysis_id, snapshot_id, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    base_sha = "a" * 40
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="lock-task",
                now=NOW,
            )
            plan = PlanVersionService(session).create_initial(
                task_id=task.id,
                content=_plan_content(),
                idempotency_key="lock-plan",
                now=NOW,
            )
            service = PlanLockService(session)
            lock = service.create(
                plan_version_id=plan.id,
                base_commit_sha=base_sha,
                idempotency_key="lock-create",
                now=NOW + timedelta(minutes=1),
            )
            replay = service.create(
                plan_version_id=plan.id,
                base_commit_sha=base_sha,
                idempotency_key="lock-create",
                now=NOW + timedelta(minutes=2),
            )
            natural_replay = service.create(
                plan_version_id=plan.id,
                base_commit_sha=base_sha,
                idempotency_key="lock-create-new-request",
                now=NOW + timedelta(minutes=3),
            )
            analysis = session.get(AnalysisVersion, analysis_id)
            assert analysis is not None
            state = ContributionTaskStateService(session).current(task.id)
            provider_hash = content_hash(
                provider_contract_payload(analysis)
            )

            assert lock.id == replay.id == natural_replay.id
            assert lock.schema_version == PLAN_LOCK_SCHEMA_VERSION
            assert lock.plan_version_id == plan.id
            assert lock.task_id == task.id
            assert lock.task_state_version_id == state.id
            assert lock.analysis_version_id == analysis_id
            assert lock.snapshot_id == snapshot_id
            assert lock.base_commit_sha == base_sha
            assert lock.task_record_hash == task.record_hash
            assert lock.task_state_record_hash == state.record_hash
            assert lock.analysis_record_hash == analysis.record_hash
            assert lock.analysis_output_hash == analysis.analysis_output_hash
            assert lock.snapshot_inputs_hash == analysis.snapshot_inputs_hash
            assert lock.provider_name == analysis.provider_name
            assert lock.inspect_policy_version == (
                analysis.inspect_policy_version
            )
            assert lock.analyze_policy_version == (
                analysis.analyze_policy_version
            )
            assert lock.provider_contract_hash == provider_hash
            assert lock.plan_content_hash == plan.content_hash
            assert lock.plan_record_hash == plan.record_hash
            assert lock.lock_hash == content_hash(
                plan_lock_payload(
                    plan_version_id=plan.id,
                    task_id=task.id,
                    task_state_version_id=state.id,
                    analysis_version_id=analysis.id,
                    snapshot_id=analysis.snapshot_id,
                    base_commit_sha=base_sha,
                    task_record_hash=task.record_hash,
                    task_state_record_hash=state.record_hash,
                    analysis_record_hash=analysis.record_hash,
                    analysis_output_hash=analysis.analysis_output_hash,
                    snapshot_inputs_hash=analysis.snapshot_inputs_hash,
                    provider_contract_hash=provider_hash,
                    plan_content_hash=plan.content_hash,
                    plan_record_hash=plan.record_hash,
                )
            )
            assert state.to_state == ContributionTaskState.PLANNING.value
            assert session.scalar(
                select(func.count()).select_from(PlanLock)
            ) == 1
            lock_id = lock.id
            lock_hash = lock.lock_hash
    finally:
        database.close()

    restarted = Database(f"sqlite+pysqlite:///{path}")
    restarted.create_schema()
    try:
        with restarted.session() as session:
            verified = PlanLockService(session).get_verified(lock_id)
            assert verified.lock_hash == lock_hash
            assert verified.base_commit_sha == base_sha
    finally:
        restarted.close()


def test_plan_lock_rejects_invalid_base_and_stale_plan(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plan-lock-stale.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="lock-stale-task",
                now=NOW,
            )
            plans = PlanVersionService(session)
            first_content = _plan_content()
            first = plans.create_initial(
                task_id=task.id,
                content=first_content,
                idempotency_key="lock-stale-v1",
            )
            second = plans.create_revision(
                parent_version_id=first.id,
                content=replace(
                    first_content,
                    goal="Use the latest plan revision",
                ),
                idempotency_key="lock-stale-v2",
            )
            locks = PlanLockService(session)
            for invalid in ("A" * 40, "a" * 39, "z" * 40):
                with pytest.raises(ValueError, match="base commit"):
                    locks.create(
                        plan_version_id=second.id,
                        base_commit_sha=invalid,
                        idempotency_key=f"lock-invalid-{len(invalid)}",
                    )
            with pytest.raises(
                PlanLockConflictError,
                match="latest",
            ):
                locks.create(
                    plan_version_id=first.id,
                    base_commit_sha="b" * 40,
                    idempotency_key="lock-stale-plan",
                )
            lock = locks.create(
                plan_version_id=second.id,
                base_commit_sha="b" * 40,
                idempotency_key="lock-latest-plan",
            )
            assert lock.plan_version_id == second.id
    finally:
        database.close()


def test_plan_lock_database_rejects_tamper_update_and_delete(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plan-lock-triggers.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="lock-trigger-task",
            )
            plan = PlanVersionService(session).create_initial(
                task_id=task.id,
                content=_plan_content(),
                idempotency_key="lock-trigger-plan",
            )
            lock = PlanLockService(session).create(
                plan_version_id=plan.id,
                base_commit_sha="c" * 40,
                idempotency_key="lock-trigger-lock",
            )
            lock_id = lock.id

        with pytest.raises(IntegrityError, match="immutable"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE plan_locks SET base_commit_sha = :sha "
                        "WHERE id = :id"
                    ),
                    {"id": lock_id, "sha": "d" * 40},
                )
        with pytest.raises(IntegrityError, match="immutable"):
            with database.engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM plan_locks WHERE id = :id"),
                    {"id": lock_id},
                )
        with pytest.raises(IntegrityError, match="provenance"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO plan_locks ("
                        "id, plan_version_id, task_id, task_state_version_id, "
                        "analysis_version_id, snapshot_id, schema_version, "
                        "idempotency_key, base_commit_sha, task_record_hash, "
                        "task_state_record_hash, analysis_record_hash, "
                        "analysis_output_hash, snapshot_inputs_hash, "
                        "provider_name, adapter_version, model_name, "
                        "model_version, inspect_prompt_version, "
                        "inspect_policy_version, inspect_output_schema_version, "
                        "analyze_prompt_version, analyze_policy_version, "
                        "analyze_output_schema_version, provider_contract_hash, "
                        "plan_content_hash, plan_record_hash, lock_hash, created_at"
                        ") SELECT "
                        ":id, plan_version_id, task_id, task_state_version_id, "
                        "analysis_version_id, snapshot_id, schema_version, :key, "
                        "base_commit_sha, task_record_hash, "
                        "task_state_record_hash, analysis_record_hash, "
                        "analysis_output_hash, snapshot_inputs_hash, "
                        ":provider_name, adapter_version, model_name, "
                        "model_version, inspect_prompt_version, "
                        "inspect_policy_version, inspect_output_schema_version, "
                        "analyze_prompt_version, analyze_policy_version, "
                        "analyze_output_schema_version, provider_contract_hash, "
                        "plan_content_hash, plan_record_hash, :lock_hash, created_at "
                        "FROM plan_locks WHERE id = :source_id"
                    ),
                    {
                        "id": "00000000-0000-0000-0000-000000000095",
                        "key": "lock-forged-provider",
                        "provider_name": "forged-provider",
                        "lock_hash": "f" * 64,
                        "source_id": lock_id,
                    },
                )
    finally:
        database.close()


def test_plan_approval_binds_lock_and_keeps_plan_immutable_across_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plan-approval.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="approval-task",
                now=NOW,
            )
            plan = PlanVersionService(session).create_initial(
                task_id=task.id,
                content=_plan_content(),
                idempotency_key="approval-plan",
                now=NOW,
            )
            lock = PlanLockService(session).create(
                plan_version_id=plan.id,
                base_commit_sha="d" * 40,
                idempotency_key="approval-lock",
                now=NOW,
            )
            approvals = PlanApprovalService(session)
            approval = approvals.approve(
                plan_lock_id=lock.id,
                idempotency_key="approval-create",
                actor_type="local_user",
                actor_id="user-1",
                action=UserAction.APPROVE_PLAN,
                now=NOW + timedelta(minutes=1),
            )
            replay = approvals.approve(
                plan_lock_id=lock.id,
                idempotency_key="approval-create",
                actor_type="local_user",
                actor_id="user-1",
                action=UserAction.APPROVE_PLAN,
                now=NOW + timedelta(minutes=2),
            )
            natural_replay = approvals.approve(
                plan_lock_id=lock.id,
                idempotency_key="approval-create-new-request",
                actor_type="local_user",
                actor_id="user-1",
                action=UserAction.APPROVE_PLAN,
                now=NOW + timedelta(minutes=3),
            )
            approved_state = ContributionTaskStateService(
                session
            ).current(task.id)

            assert approval.id == replay.id == natural_replay.id
            assert approval.schema_version == PLAN_APPROVAL_SCHEMA_VERSION
            assert approval.plan_lock_id == lock.id
            assert approval.plan_version_id == plan.id
            assert approval.task_id == task.id
            assert approval.approved_state_version_id == approved_state.id
            assert approval.actor_type == "local_user"
            assert approval.actor_id == "user-1"
            assert approval.lock_hash == lock.lock_hash
            assert approval.plan_content_hash == plan.content_hash
            assert approval.plan_record_hash == plan.record_hash
            assert approval.prior_state_record_hash == (
                lock.task_state_record_hash
            )
            assert approval.approved_state_record_hash == (
                approved_state.record_hash
            )
            assert approval.approval_hash == content_hash(
                plan_approval_payload(
                    plan_lock_id=lock.id,
                    plan_version_id=plan.id,
                    task_id=task.id,
                    approved_state_version_id=approved_state.id,
                    actor_type="local_user",
                    actor_id="user-1",
                    lock_hash=lock.lock_hash,
                    plan_content_hash=plan.content_hash,
                    plan_record_hash=plan.record_hash,
                    prior_state_record_hash=lock.task_state_record_hash,
                    approved_state_record_hash=approved_state.record_hash,
                )
            )
            assert approved_state.from_state == "planning"
            assert approved_state.to_state == "plan_approved"
            assert approved_state.reason_code == "plan_approved"
            assert session.scalar(
                select(func.count()).select_from(PlanApproval)
            ) == 1
            audit = session.scalar(
                select(AuditEvent).where(
                    AuditEvent.event_type == "plan.approved"
                )
            )
            assert audit is not None
            assert audit.actor_type == "local_user"
            assert audit.actor_id == "user-1"
            assert audit.correlation_id == "approval-create"
            assert audit.payload == plan_approval_audit_payload(
                approval=approval,
                lock=lock,
            )
            assert audit.payload_hash == content_hash(audit.payload)
            with pytest.raises(
                PlanVersionConflictError,
                match="planning state",
            ):
                PlanVersionService(session).create_revision(
                    parent_version_id=plan.id,
                    content=replace(
                        _plan_content(),
                        goal="Attempt to revise an approved plan",
                    ),
                    idempotency_key="approval-revision-rejected",
                )
            approval_id = approval.id
            approval_hash = approval.approval_hash
    finally:
        database.close()

    restarted = Database(f"sqlite+pysqlite:///{path}")
    restarted.create_schema()
    try:
        with restarted.session() as session:
            verified = PlanApprovalService(session).get_verified(approval_id)
            assert verified.approval_hash == approval_hash
            assert ContributionTaskStateService(
                session
            ).current(verified.task_id).to_state == "plan_approved"
    finally:
        restarted.close()


def test_plan_approval_audit_failure_rolls_back_approval_and_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "plan-approval-audit-failure.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="approval-audit-failure-task",
            )
            plan = PlanVersionService(session).create_initial(
                task_id=task.id,
                content=_plan_content(),
                idempotency_key="approval-audit-failure-plan",
            )
            lock = PlanLockService(session).create(
                plan_version_id=plan.id,
                base_commit_sha="a" * 40,
                idempotency_key="approval-audit-failure-lock",
            )

            def fail_audit(*args, **kwargs):
                raise RuntimeError("simulated audit failure")

            monkeypatch.setattr(
                "app.approvals.AuditService.prepare",
                fail_audit,
            )
            with pytest.raises(
                PlanApprovalConflictError,
                match="audit evidence",
            ):
                PlanApprovalService(session).approve(
                    plan_lock_id=lock.id,
                    idempotency_key="approval-audit-failure",
                    actor_type="local_user",
                    actor_id="user-1",
                    action=UserAction.APPROVE_PLAN,
                )
            assert session.scalar(
                select(func.count()).select_from(PlanApproval)
            ) == 0
            assert session.scalar(
                select(func.count()).select_from(AuditEvent)
            ) == 0
            states = list(
                session.scalars(
                    select(ContributionTaskStateVersion).where(
                        ContributionTaskStateVersion.task_id == task.id
                    )
                )
            )
            assert len(states) == 1
            assert states[0].to_state == "planning"
    finally:
        database.close()


def test_plan_approval_rejects_stale_lock_and_actor_mismatch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plan-approval-conflicts.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="approval-conflict-task",
            )
            plan = PlanVersionService(session).create_initial(
                task_id=task.id,
                content=_plan_content(),
                idempotency_key="approval-conflict-plan",
            )
            lock = PlanLockService(session).create(
                plan_version_id=plan.id,
                base_commit_sha="e" * 40,
                idempotency_key="approval-conflict-lock",
            )
            approvals = PlanApprovalService(session)
            for wrong_action in (
                UserAction.START_EXECUTION,
                UserAction.PUBLISH_DRAFT_PR,
            ):
                with pytest.raises(
                    PlanApprovalConflictError,
                    match="cannot authorize approve_plan",
                ):
                    approvals.approve(
                        plan_lock_id=lock.id,
                        idempotency_key=(
                            f"approval-wrong-action-{wrong_action.value}"
                        ),
                        actor_type="local_user",
                        actor_id="user-1",
                        action=wrong_action,
                    )
            assert session.scalar(
                select(func.count()).select_from(PlanApproval)
            ) == 0
            assert ContributionTaskStateService(
                session
            ).current(task.id).to_state == "planning"
            with pytest.raises(ValueError, match="actor ID"):
                approvals.approve(
                    plan_lock_id=lock.id,
                    idempotency_key="approval-secret-actor",
                    actor_type="local_user",
                    actor_id="ghp_approvalsecretcanary12345678",
                    action=UserAction.APPROVE_PLAN,
                )
            approval = approvals.approve(
                plan_lock_id=lock.id,
                idempotency_key="approval-conflict-create",
                actor_type="local_user",
                actor_id="user-1",
                action=UserAction.APPROVE_PLAN,
            )
            with pytest.raises(
                PlanApprovalConflictError,
                match="different inputs",
            ):
                approvals.approve(
                    plan_lock_id=lock.id,
                    idempotency_key="approval-conflict-create",
                    actor_type="local_user",
                    actor_id="user-2",
                    action=UserAction.APPROVE_PLAN,
                )
            assert approval.id

        stale_path = tmp_path / "plan-approval-stale.db"
        stale_analysis_id, _, _ = _seed_analysis(stale_path)
        stale_database = Database(f"sqlite+pysqlite:///{stale_path}")
        stale_database.create_schema()
        try:
            with stale_database.session() as session:
                stale_task = ContributionTaskService(session).create(
                    analysis_version_id=stale_analysis_id,
                    idempotency_key="approval-stale-task",
                )
                stale_plan = PlanVersionService(session).create_initial(
                    task_id=stale_task.id,
                    content=_plan_content(),
                    idempotency_key="approval-stale-plan",
                )
                stale_lock = PlanLockService(session).create(
                    plan_version_id=stale_plan.id,
                    base_commit_sha="f" * 40,
                    idempotency_key="approval-stale-lock",
                )
                current = ContributionTaskStateService(
                    session
                ).current(stale_task.id)
                ContributionTaskStateService(session).transition(
                    stale_task.id,
                    expected_sequence=current.sequence,
                    expected_record_hash=current.record_hash,
                    to_state=ContributionTaskState.PLAN_APPROVED,
                    reason_code="plan_approved",
                )
                with pytest.raises(
                    PlanApprovalConflictError,
                    match="stale",
                ):
                    PlanApprovalService(session).approve(
                        plan_lock_id=stale_lock.id,
                        idempotency_key="approval-stale-attempt",
                        actor_type="local_user",
                        actor_id="user-1",
                        action=UserAction.APPROVE_PLAN,
                    )
        finally:
            stale_database.close()
    finally:
        database.close()


def test_plan_approval_and_approved_plan_reject_database_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plan-approval-triggers.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="approval-trigger-task",
            )
            plan = PlanVersionService(session).create_initial(
                task_id=task.id,
                content=_plan_content(),
                idempotency_key="approval-trigger-plan",
            )
            lock = PlanLockService(session).create(
                plan_version_id=plan.id,
                base_commit_sha="1" * 40,
                idempotency_key="approval-trigger-lock",
            )
            approval = PlanApprovalService(session).approve(
                plan_lock_id=lock.id,
                idempotency_key="approval-trigger-approval",
                actor_type="local_user",
                actor_id="user-1",
                action=UserAction.APPROVE_PLAN,
            )
            plan_id = plan.id
            approval_id = approval.id

        for table, column, value, record_id in (
            ("plan_versions", "goal", "changed", plan_id),
            (
                "plan_approvals",
                "actor_id",
                "different-user",
                approval_id,
            ),
        ):
            with pytest.raises(IntegrityError, match="immutable"):
                with database.engine.begin() as connection:
                    connection.execute(
                        text(
                            f"UPDATE {table} SET {column} = :value "
                            "WHERE id = :id"
                        ),
                        {"id": record_id, "value": value},
                    )
        for table, record_id in (
            ("plan_versions", plan_id),
            ("plan_approvals", approval_id),
        ):
            with pytest.raises(IntegrityError, match="immutable"):
                with database.engine.begin() as connection:
                    connection.execute(
                        text(f"DELETE FROM {table} WHERE id = :id"),
                        {"id": record_id},
                    )
        with pytest.raises(IntegrityError, match="provenance"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO plan_approvals ("
                        "id, plan_lock_id, plan_version_id, task_id, "
                        "approved_state_version_id, schema_version, "
                        "idempotency_key, actor_type, actor_id, lock_hash, "
                        "plan_content_hash, plan_record_hash, "
                        "prior_state_record_hash, approved_state_record_hash, "
                        "approval_hash, created_at"
                        ") SELECT "
                        ":id, plan_lock_id, plan_version_id, task_id, "
                        "approved_state_version_id, schema_version, :key, "
                        "actor_type, actor_id, lock_hash, plan_content_hash, "
                        "plan_record_hash, :prior_hash, "
                        "approved_state_record_hash, :approval_hash, created_at "
                        "FROM plan_approvals WHERE id = :source_id"
                    ),
                    {
                        "id": "00000000-0000-0000-0000-000000000094",
                        "key": "approval-forged-prior",
                        "prior_hash": "0" * 64,
                        "approval_hash": "f" * 64,
                        "source_id": approval_id,
                    },
                )
    finally:
        database.close()


def test_plan_approval_freshness_revokes_on_any_bound_input_change(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plan-approval-freshness.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="freshness-task",
            )
            plan = PlanVersionService(session).create_initial(
                task_id=task.id,
                content=_plan_content(),
                idempotency_key="freshness-plan",
            )
            lock = PlanLockService(session).create(
                plan_version_id=plan.id,
                base_commit_sha="2" * 40,
                idempotency_key="freshness-lock",
            )
            approvals = PlanApprovalService(session)
            approval = approvals.approve(
                plan_lock_id=lock.id,
                idempotency_key="freshness-approval",
                actor_type="local_user",
                actor_id="user-1",
                action=UserAction.APPROVE_PLAN,
            )
            approved_state = ContributionTaskStateService(
                session
            ).current(task.id)
            expected = ApprovalInputFingerprint.from_lock(lock)
            fresh = approvals.check_freshness(
                approval.id,
                observed=expected,
            )
            assert fresh.valid is True
            assert fresh.reason_codes == ()
            assert fresh.expected_fingerprint_hash == (
                fresh.observed_fingerprint_hash
            )
            no_revocation = approvals.revoke_if_stale(
                approval.id,
                observed=expected,
                expected_sequence=approved_state.sequence,
                expected_state_record_hash=approved_state.record_hash,
            )
            assert no_revocation.state_version is None
            assert no_revocation.freshness.valid is True

            all_changed = replace(
                expected,
                analysis_version_id="changed-analysis",
                snapshot_id="changed-snapshot",
                base_commit_sha="3" * 40,
                provider_contract_hash="4" * 64,
                plan_version_id="changed-plan",
                plan_content_hash="5" * 64,
                plan_record_hash="6" * 64,
            )
            stale = approvals.check_freshness(
                approval.id,
                observed=all_changed,
            )
            assert stale.valid is False
            assert stale.reason_codes == (
                "analysis_version_changed",
                "snapshot_changed",
                "base_commit_changed",
                "provider_policy_changed",
                "plan_version_changed",
                "plan_content_changed",
                "plan_record_changed",
            )
            revocation = approvals.revoke_if_stale(
                approval.id,
                observed=replace(
                    expected,
                    base_commit_sha="3" * 40,
                ),
                expected_sequence=approved_state.sequence,
                expected_state_record_hash=approved_state.record_hash,
                now=NOW + timedelta(minutes=5),
            )
            assert revocation.freshness.reason_codes == (
                "base_commit_changed",
            )
            assert revocation.state_version is not None
            assert revocation.state_version.from_state == "plan_approved"
            assert revocation.state_version.to_state == "planning"
            assert revocation.state_version.reason_code == (
                "approval_stale_inputs"
            )
            assert approvals.get_verified(approval.id).id == approval.id

            revision = PlanVersionService(session).create_revision(
                parent_version_id=plan.id,
                content=replace(
                    _plan_content(),
                    goal="Revise only after the prior approval is stale",
                ),
                idempotency_key="freshness-revision",
            )
            assert revision.parent_version_id == plan.id
            with pytest.raises(
                PlanApprovalConflictError,
                match="stale",
            ):
                approvals.revoke_if_stale(
                    approval.id,
                    observed=all_changed,
                    expected_sequence=approved_state.sequence,
                    expected_state_record_hash=approved_state.record_hash,
                )
    finally:
        database.close()


def test_execution_readiness_requires_exact_current_approval_and_action(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution-readiness.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="readiness-task",
            )
            plan = PlanVersionService(session).create_initial(
                task_id=task.id,
                content=_plan_content(),
                idempotency_key="readiness-plan",
            )
            lock = PlanLockService(session).create(
                plan_version_id=plan.id,
                base_commit_sha="7" * 40,
                idempotency_key="readiness-lock",
            )
            observed = ApprovalInputFingerprint.from_lock(lock)
            readiness = ExecutionReadinessService(session)
            with pytest.raises(ExecutionReadinessError) as missing:
                readiness.assert_ready(
                    approval_id="missing-approval",
                    observed=observed,
                    action=UserAction.START_EXECUTION,
                )
            assert missing.value.reason_codes == (
                "approval_missing_or_invalid",
            )

            approval = PlanApprovalService(session).approve(
                plan_lock_id=lock.id,
                idempotency_key="readiness-approval",
                actor_type="local_user",
                actor_id="user-1",
                action=UserAction.APPROVE_PLAN,
            )
            with pytest.raises(ExecutionReadinessError) as wrong_action:
                readiness.assert_ready(
                    approval_id=approval.id,
                    observed=observed,
                    action=UserAction.APPROVE_PLAN,
                )
            assert wrong_action.value.reason_codes == ("wrong_user_action",)

            ready = readiness.assert_ready(
                approval_id=approval.id,
                observed=observed,
                action=UserAction.START_EXECUTION,
            )
            assert ready.approval_id == approval.id
            assert ready.approval_hash == approval.approval_hash
            assert ready.plan_version_id == plan.id
            assert ready.plan_record_hash == plan.record_hash
            assert ready.task_id == task.id
            assert ready.base_commit_sha == lock.base_commit_sha

            with pytest.raises(ExecutionReadinessError) as stale:
                readiness.assert_ready(
                    approval_id=approval.id,
                    observed=replace(
                        observed,
                        plan_content_hash="8" * 64,
                    ),
                    action=UserAction.START_EXECUTION,
                )
            assert stale.value.reason_codes == ("plan_content_changed",)
            assert ContributionTaskStateService(
                session
            ).current(task.id).to_state == "planning"
            with pytest.raises(ExecutionReadinessError) as revoked:
                readiness.assert_ready(
                    approval_id=approval.id,
                    observed=observed,
                    action=UserAction.START_EXECUTION,
                )
            assert revoked.value.reason_codes == (
                "plan_not_currently_approved",
            )
    finally:
        database.close()


def test_execution_stage_chain_survives_restart_and_rebuilds_exact_specs(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution-stage-restart.db"
    attempt_id, task_id, approval_id, attempt_hash = (
        _start_execution_fixture(path)
    )
    policy = SandboxPolicy()
    signer = JobSpecSigner(
        key_id="execution-test-key",
        signing_key=b"e" * 32,
    )

    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            service = ExecutionAttemptService(session)
            attempt = service.get_verified(attempt_id)
            pending = service.current(attempt_id)
            explore_spec = service.build_current_job_spec(attempt_id)
            assert attempt.record_hash == attempt_hash
            assert attempt.plan_approval_id == approval_id
            assert pending.stage == "explore"
            assert pending.status == "pending"
            assert explore_spec.execution_attempt_id == attempt_id
            assert explore_spec.correlation_id == attempt_id
            explore_spec_hash = explore_spec.spec_hash
            pending_sequence = pending.sequence
            pending_hash = pending.record_hash
    finally:
        database.close()

    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            service = ExecutionAttemptService(session)
            rebuilt = service.build_current_job_spec(attempt_id)
            assert rebuilt.spec_hash == explore_spec_hash
            signed_explore = signer.sign(rebuilt)
            running = service.mark_running(
                attempt_id,
                expected_sequence=pending_sequence,
                expected_record_hash=pending_hash,
                signed_job_spec=signed_explore,
                signer=signer,
                sandbox_policy=policy,
                idempotency_key="execution-explore-running",
                now=NOW + timedelta(minutes=1),
            )
            assert running.status == "running"
    finally:
        database.close()

    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            service = ExecutionAttemptService(session)
            replay = service.mark_running(
                attempt_id,
                expected_sequence=pending_sequence,
                expected_record_hash=pending_hash,
                signed_job_spec=signed_explore,
                signer=signer,
                sandbox_policy=policy,
                idempotency_key="execution-explore-running",
            )
            explored = service.finish_stage(
                attempt_id,
                expected_sequence=replay.sequence,
                expected_record_hash=replay.record_hash,
                status=ExecutionStageStatus.SUCCEEDED,
                reason_code="explore_succeeded",
                idempotency_key="execution-explore-succeeded",
                result_hash="1" * 64,
                now=NOW + timedelta(minutes=2),
            )
            assert explored.status == "succeeded"
    finally:
        database.close()

    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            service = ExecutionAttemptService(session)
            explored = service.current(attempt_id)
            implement_pending = service.advance_stage(
                attempt_id,
                expected_sequence=explored.sequence,
                expected_record_hash=explored.record_hash,
                input_hashes=("1" * 64, "2" * 64),
                reason_code="explore_succeeded",
                idempotency_key="execution-implement-pending",
                now=NOW + timedelta(minutes=3),
            )
            implement_spec = service.build_current_job_spec(
                attempt_id,
                allowed_change_paths=(
                    "app/plans.py",
                    "tests/test_planning.py",
                ),
            )
            signed_implement = signer.sign(implement_spec)
            implement_running = service.mark_running(
                attempt_id,
                expected_sequence=implement_pending.sequence,
                expected_record_hash=implement_pending.record_hash,
                signed_job_spec=signed_implement,
                signer=signer,
                sandbox_policy=policy,
                idempotency_key="execution-implement-running",
                workspace_id="workspace-attempt-1",
                workspace_ref=(
                    "contribos-workspace-" + "d" * 32
                ),
                now=NOW + timedelta(minutes=4),
            )
            assert implement_running.workspace_inventory_hash is None
    finally:
        database.close()

    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            service = ExecutionAttemptService(session)
            implement_running = service.current(attempt_id)
            implemented = service.finish_stage(
                attempt_id,
                expected_sequence=implement_running.sequence,
                expected_record_hash=implement_running.record_hash,
                status=ExecutionStageStatus.SUCCEEDED,
                reason_code="implement_succeeded",
                idempotency_key="execution-implement-succeeded",
                result_hash="3" * 64,
                workspace_inventory_hash="4" * 64,
                now=NOW + timedelta(minutes=5),
            )
            assert implemented.workspace_inventory_hash == "4" * 64
    finally:
        database.close()

    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            service = ExecutionAttemptService(session)
            implemented = service.current(attempt_id)
            verify_pending = service.advance_stage(
                attempt_id,
                expected_sequence=implemented.sequence,
                expected_record_hash=implemented.record_hash,
                input_hashes=("3" * 64,),
                reason_code="implement_succeeded",
                idempotency_key="execution-verify-pending",
                now=NOW + timedelta(minutes=6),
            )
            verify_spec = service.build_current_job_spec(
                attempt_id,
                commands=(
                    SandboxCommand(
                        command_id="focused-tests",
                        argv=("python", "-m", "pytest", "-q"),
                    ),
                ),
            )
            signed_verify = signer.sign(verify_spec)
            verify_running = service.mark_running(
                attempt_id,
                expected_sequence=verify_pending.sequence,
                expected_record_hash=verify_pending.record_hash,
                signed_job_spec=signed_verify,
                signer=signer,
                sandbox_policy=policy,
                idempotency_key="execution-verify-running",
                now=NOW + timedelta(minutes=7),
            )
            assert verify_running.workspace_ref == (
                "contribos-workspace-" + "d" * 32
            )
    finally:
        database.close()

    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            service = ExecutionAttemptService(session)
            verify_running = service.current(attempt_id)
            verified = service.finish_stage(
                attempt_id,
                expected_sequence=verify_running.sequence,
                expected_record_hash=verify_running.record_hash,
                status=ExecutionStageStatus.SUCCEEDED,
                reason_code="verify_succeeded",
                idempotency_key="execution-verify-succeeded",
                result_hash="5" * 64,
                now=NOW + timedelta(minutes=8),
            )
            history = service.history(attempt_id)
            assert verified.stage == "verify"
            assert verified.status == "succeeded"
            assert [item.sequence for item in history] == list(range(1, 10))
            assert [
                (item.stage, item.status)
                for item in history
            ] == [
                ("explore", "pending"),
                ("explore", "running"),
                ("explore", "succeeded"),
                ("implement", "pending"),
                ("implement", "running"),
                ("implement", "succeeded"),
                ("verify", "pending"),
                ("verify", "running"),
                ("verify", "succeeded"),
            ]
            task_state = ContributionTaskStateService(session).current(task_id)
            assert task_state.to_state == "executing"
    finally:
        database.close()


def test_execution_start_and_stage_transitions_fail_closed_and_replay(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution-transition-conflicts.db"
    attempt_id, _, approval_id, _ = _start_execution_fixture(path)
    policy = SandboxPolicy()
    signer = JobSpecSigner(
        key_id="execution-conflict-key",
        signing_key=b"f" * 32,
    )
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            service = ExecutionAttemptService(session)
            attempt = service.get_verified(attempt_id)
            lock = attempt.plan_approval.plan_lock
            replay = service.start(
                approval_id=approval_id,
                observed=ApprovalInputFingerprint.from_lock(lock),
                action=UserAction.START_EXECUTION,
                idempotency_key="execution-start",
                actor_type="local_user",
                actor_id="user-1",
                repository_archive_hash="b" * 64,
                runner_image_digest="sha256:" + "c" * 64,
                sandbox_policy=policy,
            )
            assert replay.id == attempt_id
            with pytest.raises(
                ExecutionAttemptConflictError,
                match="different inputs",
            ):
                service.start(
                    approval_id=approval_id,
                    observed=ApprovalInputFingerprint.from_lock(lock),
                    action=UserAction.START_EXECUTION,
                    idempotency_key="execution-start",
                    actor_type="local_user",
                    actor_id="user-1",
                    repository_archive_hash="9" * 64,
                    runner_image_digest="sha256:" + "c" * 64,
                    sandbox_policy=policy,
                )
            with pytest.raises(
                ExecutionAttemptConflictError,
                match="cannot authorize",
            ):
                service.start(
                    approval_id=approval_id,
                    observed=ApprovalInputFingerprint.from_lock(lock),
                    action=UserAction.APPROVE_PLAN,
                    idempotency_key="execution-wrong-action",
                    actor_type="local_user",
                    actor_id="user-1",
                    repository_archive_hash="b" * 64,
                    runner_image_digest="sha256:" + "c" * 64,
                    sandbox_policy=policy,
                )
            with pytest.raises(ValueError, match="actor ID is invalid"):
                service.start(
                    approval_id=approval_id,
                    observed=ApprovalInputFingerprint.from_lock(lock),
                    action=UserAction.START_EXECUTION,
                    idempotency_key="execution-secret-actor",
                    actor_type="local_user",
                    actor_id="ghp_executionsecretcanary1234567890",
                    repository_archive_hash="b" * 64,
                    runner_image_digest="sha256:" + "c" * 64,
                    sandbox_policy=policy,
                )

            pending = service.current(attempt_id)
            spec = service.build_current_job_spec(attempt_id)
            wrong_spec = replace(spec, base_commit_sha="9" * 40)
            with pytest.raises(
                ExecutionStageTransitionError,
                match="does not match",
            ):
                service.mark_running(
                    attempt_id,
                    expected_sequence=pending.sequence,
                    expected_record_hash=pending.record_hash,
                    signed_job_spec=signer.sign(wrong_spec),
                    signer=signer,
                    sandbox_policy=policy,
                    idempotency_key="execution-wrong-spec",
                )
            running = service.mark_running(
                attempt_id,
                expected_sequence=pending.sequence,
                expected_record_hash=pending.record_hash,
                signed_job_spec=signer.sign(spec),
                signer=signer,
                sandbox_policy=policy,
                idempotency_key="execution-valid-spec",
            )
            with pytest.raises(
                ExecutionAttemptConflictError,
                match="stale",
            ):
                service.finish_stage(
                    attempt_id,
                    expected_sequence=pending.sequence,
                    expected_record_hash=pending.record_hash,
                    status=ExecutionStageStatus.SUCCEEDED,
                    reason_code="explore_succeeded",
                    idempotency_key="execution-stale-finish",
                    result_hash="1" * 64,
                )
            with pytest.raises(
                ExecutionStageTransitionError,
                match="succeeded",
            ):
                service.advance_stage(
                    attempt_id,
                    expected_sequence=running.sequence,
                    expected_record_hash=running.record_hash,
                    input_hashes=("1" * 64, "2" * 64),
                    reason_code="illegal_advance",
                    idempotency_key="execution-illegal-advance",
                )
    finally:
        database.close()


def test_execution_attempt_and_stage_records_are_database_immutable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution-immutability.db"
    attempt_id, _, _, _ = _start_execution_fixture(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            service = ExecutionAttemptService(session)
            attempt = service.get_verified(attempt_id)
            initial = service.current(attempt_id)
        with pytest.raises(IntegrityError, match="immutable"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE execution_attempts "
                        "SET actor_id = 'changed' WHERE id = :id"
                    ),
                    {"id": attempt.id},
                )
        with pytest.raises(IntegrityError, match="immutable"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE execution_stage_versions "
                        "SET reason_code = 'changed' WHERE id = :id"
                    ),
                    {"id": initial.id},
                )
        with pytest.raises(IntegrityError, match="provenance"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO execution_stage_versions ("
                        "id, execution_attempt_id, schema_version, sequence, "
                        "stage, status, reason_code, idempotency_key, "
                        "job_spec_hash, input_hashes, result_hash, "
                        "workspace_id, workspace_ref, "
                        "workspace_inventory_hash, attempt_record_hash, "
                        "previous_stage_state_hash, record_hash, created_at"
                        ") VALUES ("
                        ":id, :attempt_id, '1', 2, 'explore', 'running', "
                        "'stage_started', 'execution-forged-stage', :job_hash, "
                        "'[]', NULL, NULL, NULL, NULL, :attempt_hash, "
                        ":previous_hash, :record_hash, :created_at"
                        ")"
                    ),
                    {
                        "id": "00000000-0000-0000-0000-000000000099",
                        "attempt_id": attempt.id,
                        "job_hash": "8" * 64,
                        "attempt_hash": attempt.record_hash,
                        "previous_hash": "7" * 64,
                        "record_hash": "6" * 64,
                        "created_at": NOW.isoformat(),
                    },
                )
        with pytest.raises(IntegrityError, match="immutable"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "DELETE FROM execution_stage_versions WHERE id = :id"
                    ),
                    {"id": initial.id},
                )
        with pytest.raises(IntegrityError, match="immutable"):
            with database.engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM execution_attempts WHERE id = :id"),
                    {"id": attempt.id},
                )
    finally:
        database.close()


def test_queued_execution_stage_cancellation_is_atomic_and_immutable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution-queued-cancel.db"
    attempt_id, _, _, _ = _start_execution_fixture(path)
    policy = SandboxPolicy()
    signer = JobSpecSigner(
        key_id="execution-cancel-key",
        signing_key=b"g" * 32,
    )
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            attempts = ExecutionAttemptService(session)
            control = ExecutionStageControlService(session)
            signed = signer.sign(
                attempts.build_current_job_spec(attempt_id)
            )
            run = control.schedule_current(
                attempt_id,
                signed_job_spec=signed,
                signer=signer,
                sandbox_policy=policy,
                idempotency_key="execution-stage-run-cancel",
                max_stage_runs=2,
                now=NOW + timedelta(minutes=1),
            )
            replay = control.schedule_current(
                attempt_id,
                signed_job_spec=signed,
                signer=signer,
                sandbox_policy=policy,
                idempotency_key="execution-stage-run-cancel",
                max_stage_runs=2,
            )
            assert replay.id == run.id
            assert run.job.state == "queued"
            run_id = run.id
            job_id = run.job_id
    finally:
        database.close()

    restarted = Database(f"sqlite+pysqlite:///{path}")
    restarted.create_schema()
    try:
        with restarted.session() as session:
            control = ExecutionStageControlService(session)
            cancelled_job = control.request_cancel(
                run_id,
                now=NOW + timedelta(minutes=2),
            )
            current = ExecutionAttemptService(session).current(attempt_id)
            assert cancelled_job.state == "cancelled"
            assert current.stage == "explore"
            assert current.status == "cancelled"
            assert current.reason_code == "cancellation_requested"
        with pytest.raises(IntegrityError, match="inputs are immutable"):
            with restarted.engine.begin() as connection:
                connection.execute(
                    text("UPDATE jobs SET payload = '{}' WHERE id = :id"),
                    {"id": job_id},
                )
        with pytest.raises(IntegrityError, match="runs are immutable"):
            with restarted.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE execution_stage_runs "
                        "SET max_stage_runs = 3 WHERE id = :id"
                    ),
                    {"id": run_id},
                )
    finally:
        restarted.close()


def test_execution_failure_worker_loss_and_retry_budget_survive_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution-worker-loss.db"
    attempt_id, _, _, _ = _start_execution_fixture(path)
    policy = SandboxPolicy()
    signer = JobSpecSigner(
        key_id="execution-retry-key",
        signing_key=b"h" * 32,
    )
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            attempts = ExecutionAttemptService(session)
            control = ExecutionStageControlService(session)
            first_signed = signer.sign(
                attempts.build_current_job_spec(attempt_id)
            )
            first = control.schedule_current(
                attempt_id,
                signed_job_spec=first_signed,
                signer=signer,
                sandbox_policy=policy,
                idempotency_key="execution-stage-run-1",
                max_stage_runs=2,
                now=NOW + timedelta(minutes=1),
            )
            leased = control.lease_next(
                worker_id="sandbox-worker-1",
                lease_seconds=10,
                now=NOW + timedelta(minutes=1),
            )
            assert leased is not None and leased.id == first.id
            control.begin(
                first.id,
                worker_id="sandbox-worker-1",
                signed_job_spec=first_signed,
                signer=signer,
                sandbox_policy=policy,
                now=NOW + timedelta(minutes=1, seconds=1),
            )
            failed = control.complete(
                first.id,
                worker_id="sandbox-worker-1",
                status=ExecutionStageStatus.FAILED,
                reason_code="sandbox_failed",
                now=NOW + timedelta(minutes=1, seconds=2),
            )
            assert failed.status == "failed"
            retry_pending = control.prepare_retry(
                first.id,
                idempotency_key="execution-stage-retry-1",
                now=NOW + timedelta(minutes=2),
            )
            assert retry_pending.status == "pending"
            second_signed = signer.sign(
                attempts.build_current_job_spec(attempt_id)
            )
            second = control.schedule_current(
                attempt_id,
                signed_job_spec=second_signed,
                signer=signer,
                sandbox_policy=policy,
                idempotency_key="execution-stage-run-2",
                max_stage_runs=2,
                now=NOW + timedelta(minutes=2),
            )
            leased = control.lease_next(
                worker_id="lost-sandbox-worker",
                lease_seconds=10,
                now=NOW + timedelta(minutes=2),
            )
            assert leased is not None and leased.id == second.id
            control.begin(
                second.id,
                worker_id="lost-sandbox-worker",
                signed_job_spec=second_signed,
                signer=signer,
                sandbox_policy=policy,
                now=NOW + timedelta(minutes=2, seconds=1),
            )
            second_run_id = second.id
    finally:
        database.close()

    restarted = Database(f"sqlite+pysqlite:///{path}")
    restarted.create_schema()
    try:
        with restarted.session() as session:
            control = ExecutionStageControlService(session)
            reconciled = control.reconcile_expired(
                now=NOW + timedelta(minutes=2, seconds=11)
            )
            assert reconciled == (second_run_id,)
            second = control.get_verified(second_run_id)
            current = ExecutionAttemptService(session).current(attempt_id)
            assert second.job.state == "timed_out"
            assert second.job.error_code == "lease_expired"
            assert current.status == "timed_out"
            assert current.reason_code == "worker_lease_expired"
            with pytest.raises(ExecutionRetryExhaustedError, match="exhausted"):
                control.prepare_retry(
                    second_run_id,
                    idempotency_key="execution-stage-retry-exhausted",
                )
            history = ExecutionAttemptService(session).history(attempt_id)
            assert [
                (item.stage, item.status)
                for item in history
            ] == [
                ("explore", "pending"),
                ("explore", "running"),
                ("explore", "failed"),
                ("explore", "pending"),
                ("explore", "running"),
                ("explore", "timed_out"),
            ]
            runs = tuple(
                session.scalars(
                    select(ExecutionStageRun)
                    .where(
                        ExecutionStageRun.execution_attempt_id == attempt_id
                    )
                    .order_by(ExecutionStageRun.stage_run_number)
                )
            )
            assert [item.stage_run_number for item in runs] == [1, 2]
            assert all(item.max_stage_runs == 2 for item in runs)
    finally:
        restarted.close()


def test_running_stage_cancellation_blocks_success_and_becomes_terminal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution-running-cancel.db"
    attempt_id, _, _, _ = _start_execution_fixture(path)
    policy = SandboxPolicy()
    signer = JobSpecSigner(
        key_id="execution-running-cancel-key",
        signing_key=b"i" * 32,
    )
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            attempts = ExecutionAttemptService(session)
            control = ExecutionStageControlService(session)
            signed = signer.sign(
                attempts.build_current_job_spec(attempt_id)
            )
            run = control.schedule_current(
                attempt_id,
                signed_job_spec=signed,
                signer=signer,
                sandbox_policy=policy,
                idempotency_key="execution-running-cancel-run",
                now=NOW + timedelta(minutes=1),
            )
            leased = control.lease_next(
                worker_id="sandbox-cancel-worker",
                lease_seconds=30,
                now=NOW + timedelta(minutes=1),
            )
            assert leased is not None and leased.id == run.id
            control.begin(
                run.id,
                worker_id="sandbox-cancel-worker",
                signed_job_spec=signed,
                signer=signer,
                sandbox_policy=policy,
                now=NOW + timedelta(minutes=1, seconds=1),
            )
            requested = control.request_cancel(
                run.id,
                now=NOW + timedelta(minutes=1, seconds=2),
            )
            assert requested.state == "running"
            assert requested.cancel_requested_at is not None
            with pytest.raises(JobTransitionError):
                control.complete(
                    run.id,
                    worker_id="sandbox-cancel-worker",
                    status=ExecutionStageStatus.SUCCEEDED,
                    reason_code="explore_succeeded",
                    result_hash="1" * 64,
                    now=NOW + timedelta(minutes=1, seconds=3),
                )
            cancelled = control.complete(
                run.id,
                worker_id="sandbox-cancel-worker",
                status=ExecutionStageStatus.CANCELLED,
                reason_code="cancellation_acknowledged",
                now=NOW + timedelta(minutes=1, seconds=4),
            )
            assert cancelled.status == "cancelled"
            assert control.get_verified(run.id).job.state == "cancelled"
    finally:
        database.close()


def test_execution_job_and_stage_terminal_outcome_roll_back_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "execution-terminal-transaction.db"
    attempt_id, _, _, _ = _start_execution_fixture(path)
    policy = SandboxPolicy()
    signer = JobSpecSigner(
        key_id="execution-transaction-key",
        signing_key=b"j" * 32,
    )
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            attempts = ExecutionAttemptService(session)
            control = ExecutionStageControlService(session)
            signed = signer.sign(
                attempts.build_current_job_spec(attempt_id)
            )
            run = control.schedule_current(
                attempt_id,
                signed_job_spec=signed,
                signer=signer,
                sandbox_policy=policy,
                idempotency_key="execution-transaction-run",
                now=NOW + timedelta(minutes=1),
            )
            control.lease_next(
                worker_id="sandbox-transaction-worker",
                lease_seconds=30,
                now=NOW + timedelta(minutes=1),
            )
            control.begin(
                run.id,
                worker_id="sandbox-transaction-worker",
                signed_job_spec=signed,
                signer=signer,
                sandbox_policy=policy,
                now=NOW + timedelta(minutes=1, seconds=1),
            )
            original = ExecutionAttemptService.finish_stage

            def fail_stage_append(*args: object, **kwargs: object) -> object:
                raise ExecutionStageTransitionError(
                    "injected stage append failure"
                )

            monkeypatch.setattr(
                ExecutionAttemptService,
                "finish_stage",
                fail_stage_append,
            )
            with pytest.raises(
                ExecutionStageTransitionError,
                match="injected",
            ):
                control.complete(
                    run.id,
                    worker_id="sandbox-transaction-worker",
                    status=ExecutionStageStatus.FAILED,
                    reason_code="sandbox_failed",
                    now=NOW + timedelta(minutes=1, seconds=2),
                )
            monkeypatch.setattr(
                ExecutionAttemptService,
                "finish_stage",
                original,
            )
            session.expire_all()
            assert control.get_verified(run.id).job.state == "running"
            assert attempts.current(attempt_id).status == "running"
            terminal = control.complete(
                run.id,
                worker_id="sandbox-transaction-worker",
                status=ExecutionStageStatus.FAILED,
                reason_code="sandbox_failed",
                now=NOW + timedelta(minutes=1, seconds=3),
            )
            assert terminal.status == "failed"
            assert control.get_verified(run.id).job.state == "failed"
    finally:
        database.close()


def test_execution_success_requires_and_atomically_binds_artifact_manifest(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution-artifact-success.db"
    artifact_root = tmp_path / "execution-artifacts"
    attempt_id, _, _, _ = _start_execution_fixture(path)
    policy = SandboxPolicy()
    signer = JobSpecSigner(
        key_id="execution-artifact-key",
        signing_key=b"k" * 32,
    )
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            attempts = ExecutionAttemptService(session)
            control = ExecutionStageControlService(session)
            signed = signer.sign(
                attempts.build_current_job_spec(attempt_id)
            )
            run = control.schedule_current(
                attempt_id,
                signed_job_spec=signed,
                signer=signer,
                sandbox_policy=policy,
                idempotency_key="execution-artifact-run",
                now=NOW + timedelta(minutes=1),
            )
            control.lease_next(
                worker_id="sandbox-artifact-worker",
                lease_seconds=30,
                now=NOW + timedelta(minutes=1),
            )
            control.begin(
                run.id,
                worker_id="sandbox-artifact-worker",
                signed_job_spec=signed,
                signer=signer,
                sandbox_policy=policy,
                now=NOW + timedelta(minutes=1, seconds=1),
            )
            bundle = _explore_artifact_bundle()
            with pytest.raises(
                ExecutionControlConflictError,
                match="requires atomic artifact finalization",
            ):
                control.complete(
                    run.id,
                    worker_id="sandbox-artifact-worker",
                    status=ExecutionStageStatus.SUCCEEDED,
                    reason_code="explore_succeeded",
                    result_hash=bundle.result_hash,
                    now=NOW + timedelta(minutes=1, seconds=2),
                )
            session.expire_all()
            with pytest.raises(
                IntegrityError,
                match="requires finalized artifacts",
            ):
                JobService(session).succeed(
                    run.job_id,
                    worker_id="sandbox-artifact-worker",
                    result_data={
                        "result_hash": bundle.result_hash,
                        "artifact_manifest_hash": "f" * 64,
                    },
                    now=NOW + timedelta(minutes=1, seconds=2),
                )
            session.rollback()
            session.expire_all()

            store = ArtifactStore(session, artifact_root)
            terminal = control.complete(
                run.id,
                worker_id="sandbox-artifact-worker",
                status=ExecutionStageStatus.SUCCEEDED,
                reason_code="explore_succeeded",
                result_hash=bundle.result_hash,
                artifact_store=store,
                artifact_bundle=bundle,
                now=NOW + timedelta(minutes=1, seconds=3),
            )
            manifest = session.scalar(
                select(ExecutionArtifactManifest).where(
                    ExecutionArtifactManifest.execution_stage_run_id
                    == run.id
                )
            )
            assert manifest is not None
            assert terminal.result_hash == bundle.result_hash
            assert control.get_verified(run.id).job.result_data == {
                "result_hash": bundle.result_hash,
                "artifact_manifest_hash": manifest.manifest_hash,
            }
            entries = tuple(
                session.scalars(
                    select(ExecutionArtifactEntry)
                    .where(
                        ExecutionArtifactEntry.manifest_id == manifest.id
                    )
                    .order_by(ExecutionArtifactEntry.position)
                )
            )
            assert [(item.position, item.role) for item in entries] == [
                (0, "stage-result")
            ]
            assert entries[0].artifact_id == bundle.result_hash
            assert session.get(Artifact, bundle.result_hash) is not None
            assert session.scalar(
                select(JobArtifact).where(
                    JobArtifact.job_id == run.job_id,
                    JobArtifact.artifact_id == bundle.result_hash,
                    JobArtifact.role == "stage-result",
                )
            ) is not None
            assert store.read_bytes(bundle.result_hash) == (
                bundle.contents[0].data
            )
            with pytest.raises(
                IntegrityError,
                match="manifests are immutable",
            ):
                session.execute(
                    text(
                        "UPDATE execution_artifact_manifests "
                        "SET result_hash = :hash WHERE id = :id"
                    ),
                    {"hash": "0" * 64, "id": manifest.id},
                )
                session.commit()
            session.rollback()
            session.expire_all()

            replay = control.complete(
                run.id,
                worker_id="sandbox-artifact-worker",
                status=ExecutionStageStatus.SUCCEEDED,
                reason_code="explore_succeeded",
                result_hash=bundle.result_hash,
                artifact_store=store,
                artifact_bundle=bundle,
            )
            assert replay.id == terminal.id

            artifact_path = artifact_root / (
                f"sha256/{bundle.result_hash[:2]}/{bundle.result_hash}"
            )
            artifact_path.write_bytes(b"tampered")
            with pytest.raises(
                ExecutionControlConflictError,
                match="hash mismatch",
            ):
                control.complete(
                    run.id,
                    worker_id="sandbox-artifact-worker",
                    status=ExecutionStageStatus.SUCCEEDED,
                    reason_code="explore_succeeded",
                    result_hash=bundle.result_hash,
                    artifact_store=store,
                    artifact_bundle=bundle,
                )
    finally:
        database.close()


def test_execution_artifact_database_rollback_leaves_recoverable_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "execution-artifact-rollback.db"
    artifact_root = tmp_path / "execution-artifact-rollback-root"
    attempt_id, _, _, _ = _start_execution_fixture(path)
    policy = SandboxPolicy()
    signer = JobSpecSigner(
        key_id="execution-artifact-rollback-key",
        signing_key=b"l" * 32,
    )
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            attempts = ExecutionAttemptService(session)
            control = ExecutionStageControlService(session)
            signed = signer.sign(
                attempts.build_current_job_spec(attempt_id)
            )
            run = control.schedule_current(
                attempt_id,
                signed_job_spec=signed,
                signer=signer,
                sandbox_policy=policy,
                idempotency_key="execution-artifact-rollback-run",
                now=NOW + timedelta(minutes=1),
            )
            control.lease_next(
                worker_id="sandbox-artifact-rollback-worker",
                lease_seconds=30,
                now=NOW + timedelta(minutes=1),
            )
            control.begin(
                run.id,
                worker_id="sandbox-artifact-rollback-worker",
                signed_job_spec=signed,
                signer=signer,
                sandbox_policy=policy,
                now=NOW + timedelta(minutes=1, seconds=1),
            )
            bundle = _explore_artifact_bundle()
            store = ArtifactStore(session, artifact_root)
            original = ExecutionAttemptService.finish_stage

            def fail_stage_append(*args: object, **kwargs: object) -> object:
                raise ExecutionStageTransitionError(
                    "injected artifact stage append failure"
                )

            monkeypatch.setattr(
                ExecutionAttemptService,
                "finish_stage",
                fail_stage_append,
            )
            with pytest.raises(
                ExecutionStageTransitionError,
                match="injected artifact",
            ):
                control.complete(
                    run.id,
                    worker_id="sandbox-artifact-rollback-worker",
                    status=ExecutionStageStatus.SUCCEEDED,
                    reason_code="explore_succeeded",
                    result_hash=bundle.result_hash,
                    artifact_store=store,
                    artifact_bundle=bundle,
                    now=NOW + timedelta(minutes=1, seconds=2),
                )
            monkeypatch.setattr(
                ExecutionAttemptService,
                "finish_stage",
                original,
            )
            session.expire_all()
            assert control.get_verified(run.id).job.state == "running"
            assert attempts.current(attempt_id).status == "running"
            assert session.scalar(
                select(func.count(ExecutionArtifactManifest.id))
            ) == 0
            assert session.scalar(select(func.count(Artifact.id))) == 0
            orphan = artifact_root / (
                f"sha256/{bundle.result_hash[:2]}/{bundle.result_hash}"
            )
            assert orphan.is_file()

            recovered = control.complete(
                run.id,
                worker_id="sandbox-artifact-rollback-worker",
                status=ExecutionStageStatus.SUCCEEDED,
                reason_code="explore_succeeded",
                result_hash=bundle.result_hash,
                artifact_store=store,
                artifact_bundle=bundle,
                now=NOW + timedelta(minutes=1, seconds=3),
            )
            assert recovered.status == "succeeded"
            assert store.read_bytes(bundle.result_hash) == (
                bundle.contents[0].data
            )
            assert session.scalar(
                select(func.count(ExecutionArtifactManifest.id))
            ) == 1
    finally:
        database.close()


def test_verify_artifacts_schedule_bounded_workspace_destruction_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution-workspace-disposal.db"
    artifact_root = tmp_path / "workspace-disposal-artifacts"
    attempt_id, _, _, _ = _start_execution_fixture(path)
    policy = SandboxPolicy()
    signer = JobSpecSigner(
        key_id="execution-cleanup-key",
        signing_key=b"m" * 32,
    )
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            attempts = ExecutionAttemptService(session)
            explore_pending = attempts.current(attempt_id)
            explore_signed = signer.sign(
                attempts.build_current_job_spec(attempt_id)
            )
            explore_running = attempts.mark_running(
                attempt_id,
                expected_sequence=explore_pending.sequence,
                expected_record_hash=explore_pending.record_hash,
                signed_job_spec=explore_signed,
                signer=signer,
                sandbox_policy=policy,
                idempotency_key="cleanup-explore-running",
            )
            explored = attempts.finish_stage(
                attempt_id,
                expected_sequence=explore_running.sequence,
                expected_record_hash=explore_running.record_hash,
                status=ExecutionStageStatus.SUCCEEDED,
                reason_code="explore_succeeded",
                idempotency_key="cleanup-explore-succeeded",
                result_hash="1" * 64,
            )
            implement_pending = attempts.advance_stage(
                attempt_id,
                expected_sequence=explored.sequence,
                expected_record_hash=explored.record_hash,
                input_hashes=("1" * 64, "2" * 64),
                reason_code="explore_succeeded",
                idempotency_key="cleanup-implement-pending",
            )
            implement_signed = signer.sign(
                attempts.build_current_job_spec(
                    attempt_id,
                    allowed_change_paths=("src/main.py",),
                )
            )
            implement_running = attempts.mark_running(
                attempt_id,
                expected_sequence=implement_pending.sequence,
                expected_record_hash=implement_pending.record_hash,
                signed_job_spec=implement_signed,
                signer=signer,
                sandbox_policy=policy,
                idempotency_key="cleanup-implement-running",
                workspace_id="workspace:cleanup-1",
                workspace_ref=(
                    "contribos-workspace-" + "d" * 32
                ),
            )
            implemented = attempts.finish_stage(
                attempt_id,
                expected_sequence=implement_running.sequence,
                expected_record_hash=implement_running.record_hash,
                status=ExecutionStageStatus.SUCCEEDED,
                reason_code="implement_succeeded",
                idempotency_key="cleanup-implement-succeeded",
                result_hash="3" * 64,
                workspace_inventory_hash="4" * 64,
            )
            verify_pending = attempts.advance_stage(
                attempt_id,
                expected_sequence=implemented.sequence,
                expected_record_hash=implemented.record_hash,
                input_hashes=("3" * 64,),
                reason_code="implement_succeeded",
                idempotency_key="cleanup-verify-pending",
            )
            verify_signed = signer.sign(
                attempts.build_current_job_spec(
                    attempt_id,
                    commands=(
                        SandboxCommand(
                            command_id="focused-tests",
                            argv=("python3", "-I", "-c", "assert True"),
                        ),
                    ),
                )
            )
            control = ExecutionStageControlService(session)
            run = control.schedule_current(
                attempt_id,
                signed_job_spec=verify_signed,
                signer=signer,
                sandbox_policy=policy,
                idempotency_key="cleanup-verify-run",
            )
            control.lease_next(
                worker_id="sandbox-verify-worker",
                lease_seconds=30,
            )
            verify_running = control.begin(
                run.id,
                worker_id="sandbox-verify-worker",
                signed_job_spec=verify_signed,
                signer=signer,
                sandbox_policy=policy,
            )
            assert verify_running.sequence == verify_pending.sequence + 1
            bundle = _verify_artifact_bundle()
            verified = control.complete(
                run.id,
                worker_id="sandbox-verify-worker",
                status=ExecutionStageStatus.SUCCEEDED,
                reason_code="verify_succeeded",
                result_hash=bundle.result_hash,
                artifact_store=ArtifactStore(session, artifact_root),
                artifact_bundle=bundle,
            )
            assert verified.status == "succeeded"
            disposal = session.scalar(
                select(ExecutionWorkspaceDisposal).where(
                    ExecutionWorkspaceDisposal.verify_stage_run_id == run.id
                )
            )
            assert disposal is not None
            disposal_id = disposal.id
            cleanup = ExecutionWorkspaceDisposalService(session)
            assert cleanup.current(disposal.id).status == "pending"
            workspace = cleanup.workspace(disposal.id)
            assert workspace.volume_name == (
                "contribos-workspace-" + "d" * 32
            )
            claimed = cleanup.claim(
                disposal.id,
                worker_id="cleanup-worker-lost",
            )
            assert claimed.status == "running"
    finally:
        database.close()

    restarted = Database(f"sqlite+pysqlite:///{path}")
    restarted.create_schema()
    try:
        with restarted.session() as session:
            cleanup = ExecutionWorkspaceDisposalService(session)
            lost = cleanup.mark_worker_lost(
                disposal_id,
                worker_id="cleanup-worker-lost",
            )
            assert lost.reason_code == "cleanup_worker_lost"
            failing = _FakeWorkspaceDestroyClient(fail=True)
            with pytest.raises(
                WorkspaceDisposalError,
                match="could not confirm",
            ) as failure:
                asyncio.run(
                    cleanup.destroy(
                        disposal_id,
                        worker_id="cleanup-worker-2",
                        client=failing,
                    )
                )
            assert "untrusted raw" not in str(failure.value)
            assert cleanup.current(disposal_id).status == "failed"

            successful = _FakeWorkspaceDestroyClient()
            completed = asyncio.run(
                cleanup.destroy(
                    disposal_id,
                    worker_id="cleanup-worker-3",
                    client=successful,
                )
            )
            assert completed.status == "succeeded"
            assert successful.calls == [
                "contribos-workspace-" + "d" * 32
            ]
            history = cleanup.history(disposal_id)
            assert [
                WorkspaceDisposalStatus(item.status) for item in history
            ] == [
                WorkspaceDisposalStatus.PENDING,
                WorkspaceDisposalStatus.RUNNING,
                WorkspaceDisposalStatus.FAILED,
                WorkspaceDisposalStatus.PENDING,
                WorkspaceDisposalStatus.RUNNING,
                WorkspaceDisposalStatus.FAILED,
                WorkspaceDisposalStatus.PENDING,
                WorkspaceDisposalStatus.RUNNING,
                WorkspaceDisposalStatus.SUCCEEDED,
            ]
            assert (
                len(
                    [
                        item
                        for item in history
                        if item.status == "running"
                    ]
                )
                == 3
            )
            replay_client = _FakeWorkspaceDestroyClient()
            replay = asyncio.run(
                cleanup.destroy(
                    disposal_id,
                    worker_id="cleanup-worker-3",
                    client=replay_client,
                )
            )
            assert replay.id == completed.id
            assert replay_client.calls == []

            with pytest.raises(
                IntegrityError,
                match="disposals are immutable",
            ):
                session.execute(
                    text(
                        "UPDATE execution_workspace_disposals "
                        "SET workspace_ref = 'changed' WHERE id = :id"
                    ),
                    {"id": disposal_id},
                )
                session.commit()
            session.rollback()
            assert session.scalar(
                select(func.count(ExecutionWorkspaceDisposalVersion.id))
            ) == 9
    finally:
        restarted.close()


def test_plan_conversation_decisions_versions_and_approval_survive_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plan-conversation-restart.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="conversation-task",
            )
            plan = PlanVersionService(session).create_initial(
                task_id=task.id,
                content=_plan_content(),
                idempotency_key="conversation-plan",
            )
            conversation = PlanConversationService(session)
            first = conversation.append_message(
                task_id=task.id,
                plan_version_id=None,
                actor_type="local_user",
                actor_id="user-1",
                text="Please make the verification steps explicit.",
                idempotency_key="conversation-message-1",
                now=NOW,
            )
            replay = conversation.append_message(
                task_id=task.id,
                plan_version_id=None,
                actor_type="local_user",
                actor_id="user-1",
                text="Please make the verification steps explicit.",
                idempotency_key="conversation-message-1",
                now=NOW + timedelta(minutes=1),
            )
            second = conversation.append_message(
                task_id=task.id,
                plan_version_id=plan.id,
                actor_type="assistant",
                actor_id="planner",
                text="The plan now lists focused and regression checks.",
                idempotency_key="conversation-message-2",
                now=NOW + timedelta(minutes=2),
            )
            decision = conversation.append_decision(
                task_id=task.id,
                plan_version_id=plan.id,
                actor_type="local_user",
                actor_id="user-1",
                decision_code=PlanDecisionCode.ACCEPT_FOR_APPROVAL,
                rationale="The acceptance criteria and risks are complete.",
                idempotency_key="conversation-decision-1",
                now=NOW + timedelta(minutes=3),
            )
            lock = PlanLockService(session).create(
                plan_version_id=plan.id,
                base_commit_sha="9" * 40,
                idempotency_key="conversation-lock",
            )
            approval = PlanApprovalService(session).approve(
                plan_lock_id=lock.id,
                idempotency_key="conversation-approval",
                actor_type="local_user",
                actor_id="user-1",
                action=UserAction.APPROVE_PLAN,
            )

            assert replay.id == first.id
            assert [entry.sequence for entry in conversation.history(task.id)] == [
                1,
                2,
                3,
            ]
            assert first.previous_entry_hash is None
            assert second.previous_entry_hash == first.record_hash
            assert decision.previous_entry_hash == second.record_hash
            assert decision.content["decision_code"] == (
                "accept_for_approval"
            )
            task_id = task.id
            plan_id = plan.id
            approval_id = approval.id
            history_hashes = [
                entry.record_hash for entry in conversation.history(task.id)
            ]
    finally:
        database.close()

    restarted = Database(f"sqlite+pysqlite:///{path}")
    restarted.create_schema()
    try:
        with restarted.session() as session:
            history = PlanConversationService(session).history(task_id)
            assert [entry.record_hash for entry in history] == history_hashes
            assert PlanVersionService(session).get_verified(plan_id).id == (
                plan_id
            )
            assert PlanApprovalService(session).get_verified(
                approval_id
            ).id == approval_id
    finally:
        restarted.close()


def test_plan_conversation_rejects_secrets_tamper_and_delete(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plan-conversation-triggers.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="conversation-trigger-task",
            )
            conversation = PlanConversationService(session)
            with pytest.raises(ValueError, match="message"):
                conversation.append_message(
                    task_id=task.id,
                    plan_version_id=None,
                    actor_type="local_user",
                    actor_id="user-1",
                    text="token ghp_conversationsecretcanary12345678",
                    idempotency_key="conversation-secret",
                )
            entry = conversation.append_message(
                task_id=task.id,
                plan_version_id=None,
                actor_type="local_user",
                actor_id="user-1",
                text="Keep the decision history append-only.",
                idempotency_key="conversation-trigger-entry",
            )
            entry_id = entry.id

        with pytest.raises(IntegrityError, match="immutable"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE plan_conversation_entries "
                        "SET actor_id = 'changed' WHERE id = :id"
                    ),
                    {"id": entry_id},
                )
        with pytest.raises(IntegrityError, match="immutable"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "DELETE FROM plan_conversation_entries WHERE id = :id"
                    ),
                    {"id": entry_id},
                )
        with pytest.raises(IntegrityError, match="provenance"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO plan_conversation_entries ("
                        "id, task_id, plan_version_id, schema_version, "
                        "sequence, entry_type, idempotency_key, actor_type, "
                        "actor_id, content, content_hash, previous_entry_hash, "
                        "record_hash, created_at"
                        ") SELECT "
                        ":id, task_id, plan_version_id, schema_version, 3, "
                        "entry_type, :key, actor_type, actor_id, content, "
                        "content_hash, :previous_hash, :record_hash, created_at "
                        "FROM plan_conversation_entries WHERE id = :source_id"
                    ),
                    {
                        "id": "00000000-0000-0000-0000-000000000093",
                        "key": "conversation-forged-chain",
                        "previous_hash": "0" * 64,
                        "record_hash": "f" * 64,
                        "source_id": entry_id,
                    },
                )
    finally:
        database.close()


@pytest.mark.parametrize(
    "factory",
    (
        lambda: replace(
            _plan_content(),
            files_to_inspect=("../host-secret",),
        ),
        lambda: PlanCommand(
            command_id="bad_argv",
            purpose="Invalid argv container",
            argv="python -m pytest",  # type: ignore[arg-type]
        ),
        lambda: PlanCommand(
            command_id="secret",
            purpose="Do not persist credentials",
            argv=("tool", "ghp_plansecretcanary12345678"),
        ),
    ),
)
def test_plan_content_rejects_unsafe_paths_commands_and_secrets(factory) -> None:
    with pytest.raises(ValueError):
        factory()


def test_initial_plan_requires_current_planning_state_and_unique_content(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plan-version-state.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="plan-state-task",
                now=NOW,
            )
            states = ContributionTaskStateService(session)
            initial = states.current(task.id)
            states.transition(
                task.id,
                expected_sequence=initial.sequence,
                expected_record_hash=initial.record_hash,
                to_state=ContributionTaskState.PLAN_APPROVED,
                reason_code="premature_approval_fixture",
            )
            with pytest.raises(
                PlanVersionConflictError,
                match="planning state",
            ):
                PlanVersionService(session).create_initial(
                    task_id=task.id,
                    content=_plan_content(),
                    idempotency_key="plan-after-state-change",
                )
            assert session.scalar(
                select(func.count()).select_from(PlanVersion)
            ) == 0
    finally:
        database.close()


def test_plan_version_database_provenance_and_immutability_triggers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plan-version-triggers.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="plan-trigger-task",
                now=NOW,
            )
            plan = PlanVersionService(session).create_initial(
                task_id=task.id,
                content=_plan_content(),
                idempotency_key="plan-trigger-plan",
                now=NOW,
            )
            plan_id = plan.id

        with pytest.raises(IntegrityError, match="immutable"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE plan_versions SET goal = 'changed' "
                        "WHERE id = :id"
                    ),
                    {"id": plan_id},
                )
        with pytest.raises(IntegrityError, match="immutable"):
            with database.engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM plan_versions WHERE id = :id"),
                    {"id": plan_id},
                )
        with pytest.raises(IntegrityError, match="provenance"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO plan_versions ("
                        "id, task_id, task_state_version_id, parent_version_id, "
                        "parent_record_hash, schema_version, version_number, "
                        "idempotency_key, task_record_hash, "
                        "task_state_record_hash, goal, acceptance_criteria, "
                        "files_to_inspect, files_likely_to_change, "
                        "implementation_steps, tests_to_add_or_run, "
                        "commands_to_run, risks, questions_for_maintainer, "
                        "content_hash, record_hash, created_at"
                        ") SELECT "
                        ":id, task_id, task_state_version_id, id, record_hash, "
                        "schema_version, 2, :key, task_record_hash, "
                        ":bad_state_hash, goal, "
                        "acceptance_criteria, files_to_inspect, "
                        "files_likely_to_change, implementation_steps, "
                        "tests_to_add_or_run, commands_to_run, risks, "
                        "questions_for_maintainer, :content_hash, :record_hash, "
                        "created_at FROM plan_versions WHERE id = :source_id"
                    ),
                    {
                        "id": "00000000-0000-0000-0000-000000000097",
                        "key": "plan-invalid-provenance",
                        "bad_state_hash": "f" * 64,
                        "content_hash": "e" * 64,
                        "record_hash": "d" * 64,
                        "source_id": plan_id,
                    },
                )
        with pytest.raises(IntegrityError, match="parent"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO plan_versions ("
                        "id, task_id, task_state_version_id, parent_version_id, "
                        "parent_record_hash, schema_version, version_number, "
                        "idempotency_key, task_record_hash, "
                        "task_state_record_hash, goal, acceptance_criteria, "
                        "files_to_inspect, files_likely_to_change, "
                        "implementation_steps, tests_to_add_or_run, "
                        "commands_to_run, risks, questions_for_maintainer, "
                        "content_hash, record_hash, created_at"
                        ") SELECT "
                        ":id, task_id, task_state_version_id, id, "
                        ":bad_parent_hash, schema_version, 2, :key, "
                        "task_record_hash, task_state_record_hash, goal, "
                        "acceptance_criteria, files_to_inspect, "
                        "files_likely_to_change, implementation_steps, "
                        "tests_to_add_or_run, commands_to_run, risks, "
                        "questions_for_maintainer, :content_hash, :record_hash, "
                        "created_at FROM plan_versions WHERE id = :source_id"
                    ),
                    {
                        "id": "00000000-0000-0000-0000-000000000096",
                        "key": "plan-invalid-parent",
                        "bad_parent_hash": "c" * 64,
                        "content_hash": "b" * 64,
                        "record_hash": "a" * 64,
                        "source_id": plan_id,
                    },
                )
    finally:
        database.close()


def test_task_state_transitions_are_legal_append_only_and_cas_guarded(
    tmp_path: Path,
) -> None:
    path = tmp_path / "contribution-task-state.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="task-state-machine",
                now=NOW,
            )
            states = ContributionTaskStateService(session)
            initial = states.current(task.id)
            approved = states.transition(
                task.id,
                expected_sequence=initial.sequence,
                expected_record_hash=initial.record_hash,
                to_state=ContributionTaskState.PLAN_APPROVED,
                reason_code="plan_approved",
                now=NOW + timedelta(minutes=1),
            )

            assert approved.sequence == 2
            assert approved.from_state == "planning"
            assert approved.to_state == "plan_approved"
            assert approved.previous_state_hash == initial.record_hash
            assert states.current(task.id).id == approved.id
            assert session.scalars(
                select(ContributionTaskStateVersion)
                .where(ContributionTaskStateVersion.task_id == task.id)
                .order_by(ContributionTaskStateVersion.sequence)
            ).all() == [initial, approved]

            with pytest.raises(TaskStateConflictError, match="stale"):
                states.transition(
                    task.id,
                    expected_sequence=initial.sequence,
                    expected_record_hash=initial.record_hash,
                    to_state=ContributionTaskState.PLAN_APPROVED,
                    reason_code="stale_approval",
                )
            with pytest.raises(TaskStateTransitionError, match="Illegal"):
                states.transition(
                    task.id,
                    expected_sequence=approved.sequence,
                    expected_record_hash=approved.record_hash,
                    to_state=ContributionTaskState.REWARDED,
                    reason_code="skip_required_gates",
                )
            with pytest.raises(ValueError, match="reason code"):
                states.transition(
                    task.id,
                    expected_sequence=approved.sequence,
                    expected_record_hash=approved.record_hash,
                    to_state=ContributionTaskState.EXECUTING,
                    reason_code="ghp_statecanary12345678",
                )

            executing = states.transition(
                task.id,
                expected_sequence=approved.sequence,
                expected_record_hash=approved.record_hash,
                to_state=ContributionTaskState.EXECUTING,
                reason_code="execution_authorized",
            )
            assert executing.sequence == 3
            assert executing.previous_state_hash == approved.record_hash

        assert LEGAL_TASK_TRANSITIONS == {
            ContributionTaskState.PLANNING: frozenset(
                {ContributionTaskState.PLAN_APPROVED}
            ),
            ContributionTaskState.PLAN_APPROVED: frozenset(
                {
                    ContributionTaskState.PLANNING,
                    ContributionTaskState.EXECUTING,
                }
            ),
            ContributionTaskState.EXECUTING: frozenset(
                {ContributionTaskState.REVIEWING}
            ),
            ContributionTaskState.REVIEWING: frozenset(
                {
                    ContributionTaskState.EXECUTING,
                    ContributionTaskState.READY,
                }
            ),
            ContributionTaskState.READY: frozenset(
                {
                    ContributionTaskState.PLANNING,
                    ContributionTaskState.DRAFT_PR,
                }
            ),
            ContributionTaskState.DRAFT_PR: frozenset(
                {
                    ContributionTaskState.CHANGES_REQUESTED,
                    ContributionTaskState.MERGED,
                }
            ),
            ContributionTaskState.CHANGES_REQUESTED: frozenset(
                {
                    ContributionTaskState.PLANNING,
                    ContributionTaskState.EXECUTING,
                }
            ),
            ContributionTaskState.MERGED: frozenset(
                {ContributionTaskState.REWARDED}
            ),
            ContributionTaskState.REWARDED: frozenset(),
        }
    finally:
        database.close()


def test_task_state_database_rejects_invalid_chain_and_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "contribution-task-state-trigger.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="task-state-trigger",
                now=NOW,
            )
            initial = ContributionTaskStateService(session).current(task.id)

        with pytest.raises(IntegrityError, match="provenance"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO contribution_task_state_versions ("
                        "id, task_id, schema_version, sequence, from_state, "
                        "to_state, reason_code, task_record_hash, "
                        "previous_state_hash, record_hash, created_at"
                        ") VALUES ("
                        ":id, :task_id, '1', 2, 'planning', 'plan_approved', "
                        "'forged_previous', :task_record_hash, :previous_hash, "
                        ":record_hash, :created_at"
                        ")"
                    ),
                    {
                        "id": "00000000-0000-0000-0000-000000000098",
                        "task_id": task.id,
                        "task_record_hash": task.record_hash,
                        "previous_hash": "f" * 64,
                        "record_hash": "e" * 64,
                        "created_at": NOW.isoformat(),
                    },
                )
        with pytest.raises(IntegrityError, match="immutable"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE contribution_task_state_versions "
                        "SET reason_code = 'changed' WHERE id = :id"
                    ),
                    {"id": initial.id},
                )
        with pytest.raises(IntegrityError, match="immutable"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "DELETE FROM contribution_task_state_versions "
                        "WHERE id = :id"
                    ),
                    {"id": initial.id},
                )
    finally:
        database.close()


def test_task_state_migration_backfills_existing_task_root(
    tmp_path: Path,
) -> None:
    path = tmp_path / "contribution-task-state-backfill.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="task-state-backfill",
                now=NOW,
            )
            task_id = task.id
            task_hash = task.record_hash
        with database.engine.begin() as connection:
            connection.execute(
                text("DROP TABLE notification_reads")
            )
            connection.execute(
                text("DROP TRIGGER in_app_notifications_no_delete")
            )
            connection.execute(
                text("DROP TRIGGER in_app_notifications_no_update")
            )
            connection.execute(text("DROP TABLE in_app_notifications"))
            connection.execute(
                text("DROP TRIGGER opportunity_disposition_versions_no_delete")
            )
            connection.execute(
                text("DROP TRIGGER opportunity_disposition_versions_no_update")
            )
            connection.execute(
                text("DROP TABLE opportunity_disposition_versions")
            )
            connection.execute(
                text("DROP TRIGGER user_preference_versions_no_delete")
            )
            connection.execute(
                text("DROP TRIGGER user_preference_versions_no_update")
            )
            connection.execute(text("DROP TABLE user_preference_versions"))
            connection.execute(
                text("DROP TRIGGER pull_request_events_no_delete")
            )
            connection.execute(
                text("DROP TRIGGER pull_request_events_no_update")
            )
            connection.execute(
                text("DROP TRIGGER pull_request_events_provenance_insert")
            )
            connection.execute(text("DROP TABLE pull_request_events"))
            connection.execute(
                text("DROP TRIGGER task_lifecycle_marks_no_delete")
            )
            connection.execute(
                text("DROP TRIGGER task_lifecycle_marks_no_update")
            )
            connection.execute(
                text("DROP TRIGGER task_lifecycle_marks_provenance_insert")
            )
            connection.execute(text("DROP TABLE task_lifecycle_marks"))
            connection.execute(
                text("DROP TRIGGER publish_confirmations_no_delete")
            )
            connection.execute(
                text("DROP TRIGGER publish_confirmations_no_update")
            )
            connection.execute(
                text("DROP TRIGGER publish_confirmations_provenance_insert")
            )
            connection.execute(text("DROP TABLE publish_confirmations"))
            connection.execute(
                text("DROP TRIGGER draft_pull_requests_no_delete")
            )
            connection.execute(
                text("DROP TRIGGER draft_pull_requests_no_update")
            )
            connection.execute(text("DROP TABLE draft_pull_requests"))
            connection.execute(text("DROP TRIGGER publish_intents_no_delete"))
            connection.execute(text("DROP TRIGGER publish_intents_no_update"))
            connection.execute(
                text("DROP TRIGGER publish_intents_provenance_insert")
            )
            connection.execute(text("DROP TABLE publish_intents"))
            connection.execute(text("DROP TRIGGER review_runs_no_delete"))
            connection.execute(text("DROP TRIGGER review_runs_no_update"))
            connection.execute(
                text("DROP TRIGGER review_runs_provenance_insert")
            )
            connection.execute(text("DROP TABLE review_runs"))
            connection.execute(
                text(
                    "DROP TRIGGER "
                    "execution_workspace_disposal_versions_no_delete"
                )
            )
            connection.execute(
                text(
                    "DROP TRIGGER "
                    "execution_workspace_disposal_versions_no_update"
                )
            )
            connection.execute(
                text(
                    "DROP TRIGGER "
                    "execution_workspace_disposal_versions_provenance_insert"
                )
            )
            connection.execute(
                text("DROP TABLE execution_workspace_disposal_versions")
            )
            connection.execute(
                text(
                    "DROP TRIGGER "
                    "execution_workspace_disposals_no_delete"
                )
            )
            connection.execute(
                text(
                    "DROP TRIGGER "
                    "execution_workspace_disposals_no_update"
                )
            )
            connection.execute(
                text(
                    "DROP TRIGGER "
                    "execution_workspace_disposals_provenance_insert"
                )
            )
            connection.execute(
                text("DROP TABLE execution_workspace_disposals")
            )
            connection.execute(
                text(
                    "DROP TRIGGER "
                    "sandbox_stage_job_success_requires_artifacts"
                )
            )
            connection.execute(
                text(
                    "DROP TRIGGER "
                    "execution_artifact_entries_no_delete"
                )
            )
            connection.execute(
                text(
                    "DROP TRIGGER "
                    "execution_artifact_entries_no_update"
                )
            )
            connection.execute(
                text(
                    "DROP TRIGGER "
                    "execution_artifact_entries_provenance_insert"
                )
            )
            connection.execute(
                text("DROP TABLE execution_artifact_entries")
            )
            connection.execute(
                text(
                    "DROP TRIGGER "
                    "execution_artifact_manifests_no_delete"
                )
            )
            connection.execute(
                text(
                    "DROP TRIGGER "
                    "execution_artifact_manifests_no_update"
                )
            )
            connection.execute(
                text(
                    "DROP TRIGGER "
                    "execution_artifact_manifests_provenance_insert"
                )
            )
            connection.execute(
                text("DROP TABLE execution_artifact_manifests")
            )
            connection.execute(
                text("DROP TRIGGER execution_stage_jobs_payload_no_update")
            )
            connection.execute(
                text("DROP TRIGGER execution_stage_runs_no_delete")
            )
            connection.execute(
                text("DROP TRIGGER execution_stage_runs_no_update")
            )
            connection.execute(
                text("DROP TRIGGER execution_stage_runs_provenance_insert")
            )
            connection.execute(text("DROP TABLE execution_stage_runs"))
            connection.execute(
                text("DROP TRIGGER execution_stage_versions_no_delete")
            )
            connection.execute(
                text("DROP TRIGGER execution_stage_versions_no_update")
            )
            connection.execute(
                text(
                    "DROP TRIGGER "
                    "execution_stage_versions_provenance_insert"
                )
            )
            connection.execute(text("DROP TABLE execution_stage_versions"))
            connection.execute(
                text("DROP TRIGGER execution_attempts_no_delete")
            )
            connection.execute(
                text("DROP TRIGGER execution_attempts_no_update")
            )
            connection.execute(
                text("DROP TRIGGER execution_attempts_provenance_insert")
            )
            connection.execute(text("DROP TABLE execution_attempts"))
            connection.execute(
                text("DROP TRIGGER plan_conversation_entries_no_delete")
            )
            connection.execute(
                text("DROP TRIGGER plan_conversation_entries_no_update")
            )
            connection.execute(
                text(
                    "DROP TRIGGER "
                    "plan_conversation_entries_provenance_insert"
                )
            )
            connection.execute(text("DROP TABLE plan_conversation_entries"))
            connection.execute(text("DROP TRIGGER plan_approvals_no_delete"))
            connection.execute(text("DROP TRIGGER plan_approvals_no_update"))
            connection.execute(
                text("DROP TRIGGER plan_approvals_provenance_insert")
            )
            connection.execute(text("DROP TABLE plan_approvals"))
            connection.execute(text("DROP TRIGGER plan_locks_no_delete"))
            connection.execute(text("DROP TRIGGER plan_locks_no_update"))
            connection.execute(
                text("DROP TRIGGER plan_locks_provenance_insert")
            )
            connection.execute(text("DROP TABLE plan_locks"))
            connection.execute(text("DROP TRIGGER plan_versions_no_delete"))
            connection.execute(text("DROP TRIGGER plan_versions_no_update"))
            connection.execute(
                text("DROP TRIGGER plan_versions_provenance_insert")
            )
            connection.execute(text("DROP TABLE plan_versions"))
            connection.execute(
                text("DROP TRIGGER contribution_task_state_no_delete")
            )
            connection.execute(
                text("DROP TRIGGER contribution_task_state_no_update")
            )
            connection.execute(
                text("DROP TRIGGER contribution_task_state_legal_insert")
            )
            connection.execute(
                text("DROP TRIGGER contribution_task_state_provenance_insert")
            )
            connection.execute(
                text("DROP TABLE contribution_task_state_versions")
            )
            connection.execute(
                text(
                    "DELETE FROM _schema_migrations "
                    "WHERE revision IN ("
                    "'0010_contribution_task_states', '0011_plan_versions', "
                    "'0012_plan_revision_links', '0013_plan_locks', "
                    "'0014_plan_approvals', '0015_plan_conversations', "
                    "'0016_execution_attempts', "
                    "'0017_execution_stage_runs', "
                    "'0018_dependency_verify_inputs', "
                    "'0019_execution_artifact_manifests', "
                    "'0020_execution_workspace_disposals', "
                    "'0021_review_runs', "
                    "'0022_publish_intents', "
                    "'0023_pull_request_events', "
                    "'0024_task_side_states', "
                    "'0025_product_experience'"
                    ")"
                )
            )

        report = database.create_schema()
        assert report.applied == (
            "0010_contribution_task_states",
            "0011_plan_versions",
            "0012_plan_revision_links",
            "0013_plan_locks",
            "0014_plan_approvals",
            "0015_plan_conversations",
            "0016_execution_attempts",
            "0017_execution_stage_runs",
            "0018_dependency_verify_inputs",
            "0019_execution_artifact_manifests",
            "0020_execution_workspace_disposals",
            "0021_review_runs",
            "0022_publish_intents",
            "0023_pull_request_events",
            "0024_task_side_states",
            "0025_product_experience",
        )
        with database.session() as session:
            current = ContributionTaskStateService(session).current(task_id)
            assert current.sequence == 1
            assert current.to_state == "planning"
            assert current.task_record_hash == task_hash
            assert current.record_hash == content_hash(
                task_state_record_payload(
                    task_id=task_id,
                    task_record_hash=task_hash,
                    sequence=1,
                    from_state=None,
                    to_state=ContributionTaskState.PLANNING,
                    reason_code="created_from_analysis",
                    previous_state_hash=None,
                )
            )
    finally:
        database.close()


def test_contribution_task_database_provenance_and_immutability_triggers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "contribution-task-triggers.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="task-trigger-check",
                now=NOW,
            )
            task_id = task.id

        with pytest.raises(IntegrityError, match="immutable"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE contribution_tasks "
                        "SET opportunity_id = opportunity_id + 1 "
                        "WHERE id = :id"
                    ),
                    {"id": task_id},
                )
        with pytest.raises(IntegrityError, match="immutable"):
            with database.engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM contribution_tasks WHERE id = :id"),
                    {"id": task_id},
                )
        with pytest.raises(IntegrityError, match="provenance"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO contribution_tasks ("
                        "id, analysis_version_id, snapshot_id, opportunity_id, "
                        "schema_version, idempotency_key, analysis_record_hash, "
                        "analysis_output_hash, snapshot_inputs_hash, record_hash, "
                        "created_at"
                        ") SELECT "
                        ":id, analysis_version_id, snapshot_id, opportunity_id, "
                        "schema_version, :key, :bad_hash, analysis_output_hash, "
                        "snapshot_inputs_hash, :record_hash, created_at "
                        "FROM contribution_tasks WHERE id = :source_id"
                    ),
                    {
                        "id": "00000000-0000-0000-0000-000000000099",
                        "key": "task-invalid-provenance",
                        "bad_hash": "f" * 64,
                        "record_hash": "e" * 64,
                        "source_id": task_id,
                    },
                )
    finally:
        database.close()
