"""Do not treat a CLI help response as proof a package check executed."""
from copy import deepcopy
from dataclasses import replace

import pytest
from sqlalchemy import func, select

from app.execution_outcome import baseline_outcome, classify_verification
from app.plans import PlanCommand
from app.verification_commands import verification_command_error


@pytest.mark.parametrize("argv", [
    ("bun", "--cwd", "packages/lib", "run", "check:types"),
    ("bun", "--cwd=packages/lib", "run", "check:types"),
    ("bun", "run", "--cwd", "packages/lib", "check:types"),
])
def test_bun_script_directory_is_explicit_even_without_manifest_content(argv):
    command = PlanCommand("types", "Check package types", argv)
    inventory = {"packages/lib/package.json"}
    error = verification_command_error([command], inventory, set())
    assert error["constraint"] == "bun_script_directory_requires_working_directory"
    fixed = replace(command, argv=("bun", "run", "check:types"), working_directory="packages/lib")
    evidence = {"entries":[{"path":"packages/lib/package.json", "content":'{"scripts":{"check:types":"tsgo --noEmit"}}'}]}
    assert verification_command_error([fixed], inventory, set(), evidence) is None
    forwarded = replace(fixed, argv=(*fixed.argv, "--cwd", "fixtures"))
    assert verification_command_error([forwarded], inventory, set(), evidence) is None
    missing = replace(fixed, argv=("bun", "run", "typecheck"))
    assert verification_command_error([missing], inventory, set(), evidence)["constraint"] == "project_script_missing"


@pytest.mark.parametrize("stream", ["stdout_log", "stderr_log"])
def test_zero_exit_help_is_reclassified_without_rewriting_historical_evidence(stream):
    evidence = [
        {"command_id":"tests", "status":"passed", "exit_code":0, "stdout_log":"14 pass"},
        {"command_id":"types", "status":"passed", "exit_code":0,
         stream:"Usage: bun run [flags] <file or script>\n\nFlags:\n..."},
    ]
    value = {"succeeded":True, "test_results":[{"outcome":"passed", "exit_code":0}]*2,
             "command_evidence":evidence}
    frozen = deepcopy(value)
    for outcome in (classify_verification(value), baseline_outcome({"inspection":{"command_evidence":evidence}})):
        assert outcome["status"] == "environment_blocked"
        assert (outcome["passed"], outcome["total"]) == (1, 2)
        assert outcome["missing_dependencies"] == ["verification_command_not_executed"]
        assert outcome["failures"][0]["status"] == "execution_unproven"
        assert outcome["failures"][0]["exit_code"] == 0
    assert value == frozen


def test_script_output_quoting_help_in_a_test_does_not_change_verdict():
    result = classify_verification({"succeeded":True, "test_results":[{"outcome":"passed", "exit_code":0}],
        "command_evidence":[{"command_id":"tests", "status":"passed", "exit_code":0,
        "stdout_log":"test expected output: Usage: bun run [flags] <file or script>\n1 pass"}]})
    assert result["status"] == "passed"


def test_legacy_plan_cannot_authorize_or_queue_execution_after_policy_correction(tmp_path, monkeypatch):
    from app.contribution_workflow import authorize_execution
    from app.execution_preflight import task_preflight
    from app.models import Job, PlanApproval, PlanLock, ExecutionAttempt
    from app.planner import PlanningService
    from app.schemas import PlanVersionCreateRequest
    from app.workbench import WorkbenchError
    from app.workbench_view import workbench_view
    from tests.test_workbench import seeded
    db, settings, task, content, context = seeded(tmp_path)
    content = replace(content, commands_to_run=(PlanCommand("types", "Check types",
        ("bun", "--cwd", "packages/lib", "run", "check:types")),))
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        # Seed historical acceptance under its original policy, then restore
        # the new policy before testing admission. No old plan is rewritten.
        with monkeypatch.context() as old:
            old.setattr("app.verification_commands.verification_command_error", lambda *args: None)
            plan = wb.persist_plan(task, PlanVersionCreateRequest.model_validate(content.hash_payload()),
                context, key="historical-plan", actor="provider")
            session.commit()
        report = task_preflight(wb, task)
        assert report["status"] == "blocked"
        assert "bun_script_directory_requires_working_directory" in {i.get("reason_code") for i in report["issues"]}
        view = workbench_view(wb, task)
        assert not view["actions"]["can_execute"]
        assert any("Bun 脚本须使用明确的工作目录" in b["message"] for b in view["readiness"]["blockers"])
        models = (Job, PlanApproval, PlanLock, ExecutionAttempt)
        before = [session.scalar(select(func.count()).select_from(m)) for m in models]
        with pytest.raises(WorkbenchError):
            authorize_execution(wb, task, plan_id=plan.id, plan_hash=plan.record_hash, key="must-reject", settings=settings)
        assert before == [session.scalar(select(func.count()).select_from(m)) for m in models]
        assert wb.latest_plan(task).record_hash == plan.record_hash
        assert not wb.latest(task, "execution_authorized")
