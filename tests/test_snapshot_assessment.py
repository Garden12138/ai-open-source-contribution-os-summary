"""The discussion entry point must work without any model report or invocation."""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.api import create_app
from app.config import Settings
from app.database import Database
from app.models import AnalysisVersion, Job, OpportunitySnapshot, ProviderInvocation
from app.planner import PlanningService, frozen_inputs
from app.planning import ContributionTaskConflictError, ContributionTaskService
from app.providers.snapshot_assessment import assessment_values
from tests.test_analysis_evidence import _scan
from tests.test_workbench import seeded, result, run_turn


def test_snapshot_entry_requires_no_model_and_keeps_reports_optional(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'basis.db'}")
    db = Database(settings.database_url)
    db.create_schema()
    _scan(db, count=35)
    with db.session() as session:
        snapshots = list(session.scalars(select(OpportunitySnapshot).order_by(OpportunitySnapshot.id)))
        from app.providers.evidence import AnalysisInputFreezer
        top30 = AnalysisInputFreezer(session).freeze_top_candidates(scan_run_id=snapshots[0].scan_run_id)
        top_ids = {item.snapshot_id for item in top30}
        assert len(top_ids) == 30
        outside = next(snapshot for snapshot in snapshots if snapshot.id not in top_ids)
        snapshot_id = outside.id
        opportunity_id = outside.opportunity_id
    with TestClient(create_app(settings)) as client:
        meta = client.get("/api/v1/meta").json()
        headers = {"Origin": "http://testserver", "X-CSRF-Token": meta["csrf_token"], "Idempotency-Key": "basis-task"}
        before = client.get("/api/v1/recommendations").json()
        created = client.post("/api/v1/tasks", json={"snapshot_id": snapshot_id}, headers=headers)
        assert created.status_code == 201, created.text
        task = created.json()
        replay = client.post("/api/v1/tasks", json={"snapshot_id": snapshot_id}, headers=headers)
        assert replay.json()["id"] == task["id"]
        assert client.get("/api/v1/recommendations").json()["analyzed_total"] == before["analyzed_total"] == 0
        assert client.get("/api/v1/contributions/dashboard").json()["funnel"]["analyzed"] == 0
        history = client.get(f"/api/v1/opportunities/{opportunity_id}/analyses").json()
        assert history["versions"][0]["job_id"] is None
        assert history["versions"][0]["provider_name"] == "snapshot_facts"
        document = client.get(f"/api/v1/opportunities/{opportunity_id}/analyses/{task['analysis_version_id']}/document")
        assert document.status_code == 200 and "尚未进行 AI 深入评估" in document.text
        for payload in ({}, {"analysis_version_id": task["analysis_version_id"], "snapshot_id": snapshot_id}):
            assert client.post("/api/v1/tasks", json=payload, headers=headers).status_code == 422
        assert client.post("/api/v1/tasks", json={"snapshot_id": "missing"}, headers=headers).status_code == 404
    with db.session() as session:
        assert list(session.scalars(select(Job))) == []
        assert list(session.scalars(select(ProviderInvocation))) == []
        verified = ContributionTaskService(session).get_verified(task["id"])
        inputs = frozen_inputs(session, verified.id)
        assert inputs["analysis"]["assessment_status"] == "unassessed"
        assert "estimated_effort" not in inputs["analysis"]
        assert "recommendation" not in inputs["analysis"]
        assert len(list(session.scalars(select(AnalysisVersion)))) == 1
    db.close()


def test_upgrade_preserves_old_analysis_tasks_and_plan_hashes(tmp_path, monkeypatch):
    import app.migrations.versions as versions
    from tests.test_planning import _seed_analysis, _plan_content
    from app.plans import PlanVersionService
    from app.providers.history import AnalysisHistoryService
    from app.providers.analysis_documents import render_analysis_markdown
    migrations = versions.MIGRATIONS
    path = tmp_path / "old-reports.db"
    with monkeypatch.context() as patch:
        patch.setattr(versions, "MIGRATIONS", migrations[:32])
        analysis_id, _, _ = _seed_analysis(path)
        db = Database(f"sqlite:///{path}")
        with db.session() as session:
            task = ContributionTaskService(session).create(analysis_version_id=analysis_id, idempotency_key="legacy-task")
            task_id, task_hash = task.id, task.record_hash
            analysis_hash = session.get(AnalysisVersion, analysis_id).record_hash
            plan = PlanVersionService(session).create_initial(task_id=task_id, content=_plan_content(), idempotency_key="old-plan")
            plan_id, plan_hash = plan.id, plan.record_hash
    assert db.create_schema().applied == ("0033_snapshot_assessments", "0034_execution_baselines", "0035_workspace_scopes", "0036_verification_environment_recovery", "0037_baseline_feedback")
    with db.session() as session:
        task = ContributionTaskService(session).get_verified(task_id)
        assert task.record_hash == task_hash
        assert PlanVersionService(session).get_verified(plan_id).record_hash == plan_hash
        analysis = session.get(AnalysisVersion, analysis_id)
        assert analysis.record_hash == analysis_hash and analysis.job_id
        detail = AnalysisHistoryService(session).get_detail(analysis.id)
        assert "开源贡献机会分析" in render_analysis_markdown(detail)
        basis_task = ContributionTaskService(session).create_from_snapshot(snapshot_id=task.snapshot_id, idempotency_key="new-basis")
        assert basis_task.id != task.id
        assert session.get(AnalysisVersion, analysis_id).record_hash == analysis_hash
        assert ContributionTaskService(session).get_verified(task_id).record_hash == task_hash
    db.close()


def test_snapshot_basis_rejects_changed_inputs_and_illegal_origin(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'integrity.db'}")
    db.create_schema()
    _scan(db, count=1)
    with db.session() as session:
        snapshot = session.scalar(select(OpportunitySnapshot))
        task = ContributionTaskService(session).create_from_snapshot(snapshot_id=snapshot.id, idempotency_key="basis")
        analysis = session.get(AnalysisVersion, task.analysis_version_id)
        analysis.structured_output = {**analysis.structured_output, "recommendation": "pursue"}
        with pytest.raises(ContributionTaskConflictError):
            ContributionTaskService(session).get_verified(task.id)
        session.rollback()
        original = session.get(AnalysisVersion, task.analysis_version_id)
        fields = {column.name: getattr(original, column.name) for column in AnalysisVersion.__table__.columns}
        fields.update(id="invalid-origin", record_hash="f" * 64, provider_name="model")
        session.add(AnalysisVersion(**fields))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
        with pytest.raises(IntegrityError):
            session.execute(text("UPDATE analysis_versions SET output_tokens = 1 WHERE id=:id"), {"id": original.id})
        session.rollback()
        snapshot = session.get(OpportunitySnapshot, task.snapshot_id)
        from app.models import ScoreVersion
        score = session.get(ScoreVersion, original.score_version_id)
        snapshot.issue_data = {**snapshot.issue_data, "title": "changed"}
        with pytest.raises(ValueError, match="verified"):
            assessment_values(snapshot, score)
    db.close()


@pytest.mark.parametrize("outcome", ["message", "clarify", "read", "deferred_plan", "invalid_plan", "invalid_command", "too_many_commands"])
def test_conversation_routes_independent_actions_without_execution(tmp_path, outcome):
    db, settings, task_id, content, _ = seeded(tmp_path)
    value = json.loads(result(content))
    if outcome == "message":
        value["plan"] = None
    elif outcome == "clarify":
        value["questions"] = [{"id": "scope", "prompt": "先确认范围"}]
        value["read_paths"] = [content.files_to_inspect[0]]
    elif outcome in {"read", "deferred_plan"}:
        value["read_paths"] = [content.files_to_inspect[0]]
        if outcome == "read":
            value["plan"] = None
    elif outcome == "invalid_command":
        value["plan"]["commands_to_run"][0]["working_directory"] = "../escape"
    elif outcome == "too_many_commands":
        command = value["plan"]["commands_to_run"][0]
        value["plan"]["commands_to_run"] = [{**command, "command_id": f"test_{i}"} for i in range(21)]
    else:
        value["plan"]["implementation_steps"] = [{"description": "invalid draft"}]
    with db.session() as session:
        PlanningService(session, settings.artifact_root).request(task_id, text="讨论", parent_id=None, expected_hash=None, key=outcome)
    completed, runner = run_turn(db, settings, json.dumps(value))
    assert completed.state == "succeeded"
    assert len(runner.invocations) == 1
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        reply = wb.latest(task_id, "assistant_message").payload
        if outcome not in {"invalid_plan", "invalid_command", "too_many_commands"}:
            assert value["reply"] in reply["reply"]
        assert wb.latest_plan(task_id) is None
        assert not wb.latest(task_id, "execution_authorized")
        if outcome == "clarify":
            assert reply["action"] == "clarify" and reply["plan_deferred"]
            assert not any(j.kind == "planning_context" for j in wb.jobs(task_id))
        elif outcome in {"read", "deferred_plan"}:
            assert reply["action"] == "read"
            assert any(j.kind == "planning_context" for j in wb.jobs(task_id))
        elif outcome in {"invalid_plan", "invalid_command", "too_many_commands"}:
            assert reply["action"] == "revise_plan" and reply["plan"] is None
            assert reply["plan_error"]["fields"] == (["implementation_steps"] if outcome == "invalid_plan" else
                ["commands_to_run.working_directory"] if outcome == "invalid_command" else ["commands_to_run"])
            assert reply["plan_receipt"]["status"] == "rejected"
        else:
            assert reply["action"] == "message"
    db.close()


def test_unread_plan_files_are_requested_without_silently_deleting_changes(tmp_path):
    db, settings, task_id, content, _ = seeded(tmp_path)
    unread = "extra.py"
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        context = wb.latest(task_id, "context_ready")
        evidence = wb.read(context.payload["artifact_id"])
        evidence["inventory"].append(unread)
        artifact_id = wb.artifact(evidence)
        wb.append(task_id, "context_ready", {**context.payload, "artifact_id": artifact_id}, key="unread-context")
        wb.request(task_id, text="修改额外文件", parent_id=None, expected_hash=None, key="unread")
    value = json.loads(result(content))
    value["plan"]["files_likely_to_change"].append(unread)
    completed, _ = run_turn(db, settings, json.dumps(value))
    assert completed.state == "succeeded"
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        reply = wb.latest(task_id, "assistant_message").payload
        assert reply["read_paths"] == [unread]
        assert unread in reply["plan"]["files_likely_to_change"]
        assert reply["plan_deferred"] and wb.latest_plan(task_id) is None
    db.close()


def test_rejected_draft_blocks_old_plan_but_valid_manual_revision_restores_readiness(tmp_path):
    from app.contribution_workflow import authorize_execution
    from app.planner import PlanningPlan
    from app.workbench import WorkbenchError
    from app.workbench_view import workbench_view

    db, settings, task_id, content, context = seeded(tmp_path)
    with db.session() as session:
        PlanningService(session, settings.artifact_root).request(
            task_id, text="制定方案", parent_id=None, expected_hash=None, key="initial")
    completed, _ = run_turn(db, settings, result(content))
    assert completed.state == "succeeded"
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        original = wb.latest_plan(task_id)
        original_id, original_hash = original.id, original.record_hash
        wb.request(task_id, text="修订方案", parent_id=original.id, expected_hash=original.record_hash, key="invalid-draft")
    value = json.loads(result(content))
    value["plan"]["commands_to_run"][0]["working_directory"] = "../escape"
    rejected, _ = run_turn(db, settings, json.dumps(value))
    assert rejected.state == "succeeded"
    # A user can stop bounded automatic correction and save a manual revision.
    from app.jobs import JobService
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        for job in wb.jobs(task_id):
            if job.state == "queued":
                JobService(session).request_cancel(job.id)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        view = workbench_view(wb, task_id)
        assert not view["actions"]["can_execute"]
        assert "planning_plan_invalid" in {b["code"] for b in view["readiness"]["blockers"]}
        with pytest.raises(WorkbenchError) as error:
            authorize_execution(wb, task_id, plan_id=original_id, plan_hash=original_hash, key="blocked", settings=settings)
        assert error.value.code == "planning_plan_invalid"
        assert not wb.latest(task_id, "execution_authorized")
        draft = PlanningPlan.model_validate(json.loads(result(content))["plan"])
        draft.parent_version_id = original_id
        draft.goal += "（人工修订）"
        saved = wb.save(task_id, draft, context_hash=context.record_hash, key="manual-repair")
        session.commit()
        assert saved.id != original_id
        assert not wb.plan_rejection_active(task_id, saved.id)
        assert workbench_view(wb, task_id)["actions"]["can_execute"]
        from app.plans import PlanVersionService
        assert PlanVersionService(session).get_verified(original_id).record_hash == original_hash
        assert not wb.latest(task_id, "execution_authorized")
    db.close()
