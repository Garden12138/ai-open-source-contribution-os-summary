"""Preserve actionable failures without retaining arbitrary subprocess output."""
import asyncio
import json
from dataclasses import replace

import pytest

from app.sandbox_worker.dependency_diagnostics import summarize_dependency_failure, validate_dependency_diagnostic
from app.sandbox_worker.dependencies import DockerDependencyRuntime, DependencyPreparationError


@pytest.mark.parametrize("output,reason",[
    ("ERROR: No matching distribution found for timelib>=0.3.0", "no_matching_distribution"),
    ("ERROR: ResolutionImpossible: conflicting dependencies", "dependency_conflict"),
    ("ProxyError: tunnel connection failed: 403 Forbidden", "proxy_denied"),
    ("ProxyError: connection failed", "network_unavailable"),
    ("OSError: No space left on device", "disk_exhausted"),
])
def test_classification_drops_raw_output_and_secrets(output,reason):
    raw = "authorization: Bearer dependencydiagnosticcanary\n" + output
    diagnostic = validate_dependency_diagnostic(summarize_dependency_failure("runtime_resolve",1,raw))
    assert diagnostic["reason_code"] == reason
    assert "dependencydiagnosticcanary" not in json.dumps(diagnostic)
    assert diagnostic["packages"] == (["timelib"] if reason == "no_matching_distribution" else [])


@pytest.mark.parametrize("phase",["project_build","project_metadata","upstream_source_build"])
def test_backend_tool_path_error_has_safe_actionable_diagnostic(phase):
    from app.sandbox_worker.dependency_diagnostics import dependency_diagnostic_message
    output="Traceback ghp_DEPENDENCYDIAGNOSTICCANARY1234567890\nRuntimeError: uv-build was not properly installed"
    diagnostic=validate_dependency_diagnostic(summarize_dependency_failure(phase,1,output))
    assert diagnostic["reason_code"] == "build_tool_unavailable"
    assert diagnostic["packages"] == ["uv-build"]
    message=dependency_diagnostic_message(diagnostic)
    assert "工具路径" in message and "uv-build" in message
    assert "CANARY" not in json.dumps(diagnostic) and "Traceback" not in message


def test_pinned_native_asset_failure_is_environment_blocked_without_network_retry():
    from app.execution_outcome import classify_verification
    from app.sandbox_worker.dependency_diagnostics import dependency_diagnostic_message
    output="PinnedDownloadError: failed to download the pinned ffmpeg build; dependencydiagnosticcanary"
    diagnostic=validate_dependency_diagnostic(summarize_dependency_failure("baseline_verify",1,output))
    assert diagnostic["reason_code"]=="native_tool_unavailable"
    assert diagnostic["packages"]==["ffmpeg","ffprobe"]
    assert "断网" in dependency_diagnostic_message(diagnostic)
    assert "dependencydiagnosticcanary" not in json.dumps(diagnostic)
    result=classify_verification({"succeeded":False,"test_results":[{"outcome":"failed","exit_code":1}],
        "command_evidence":[{"status":"failed","exit_code":1,"stdout_log":output}]})
    assert result["status"]=="environment_blocked"
    assert result["missing_dependencies"]==["ffmpeg","ffprobe"]


@pytest.mark.parametrize("invalid",[
    {"phase":"unknown"}, {"packages":["pkg @ https://untrusted.invalid/"]},
    {"packages":["ghp_dependencydiagnosticcanary"]}, {"raw_log":"sensitive"},
])
def test_unknown_or_sensitive_diagnostic_is_rejected(invalid):
    value = summarize_dependency_failure("runtime_resolve",1,"No matching distribution found for timelib>=0.3.0")
    with pytest.raises(ValueError):
        validate_dependency_diagnostic({**value,**invalid})


@pytest.mark.parametrize("valid",[True,False])
def test_docker_boundary_accepts_only_structured_failures(monkeypatch, valid):
    diagnostic = summarize_dependency_failure("build_tools_resolve",1,"No matching distribution found for examplebuild>=1")
    class Process:
        returncode = 95
        async def communicate(self):
            return ((json.dumps(diagnostic).encode(),b"") if valid else
                (b'{"raw":"dependencydiagnosticcanary"}',b"Bearer dependencydiagnosticcanary"))
    async def start(*args,**kwargs): return Process()
    monkeypatch.setattr(asyncio,"create_subprocess_exec",start)
    with pytest.raises(DependencyPreparationError) as captured:
        asyncio.run(DockerDependencyRuntime(docker_environment={})._run(("docker",),container_name="fixture",timeout_seconds=1))
    assert captured.value.code == "environment_dependency_resolution_failed"
    assert captured.value.diagnostic == (diagnostic if valid else None)
    assert "dependencydiagnosticcanary" not in captured.value.safe_message
    if valid:
        assert "构建工具" in captured.value.safe_message and "examplebuild" in captured.value.safe_message


@pytest.mark.parametrize('native',[False,True])
def test_failed_environment_receipt_blocks_same_inputs_across_plan_edits(tmp_path,native):
    from tests.test_workbench import seeded
    from app.planner import PlanningService
    from app.schemas import PlanVersionCreateRequest
    from app.execution_environment import task_environment
    from app.execution_preflight import task_preflight, environment_inputs_hash
    from app.contribution_workflow import authorize_execution
    from app.workbench import WorkbenchError
    from app.models import Job, PlanApproval
    from sqlalchemy import func, select
    db,settings,task,content,context = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session,settings.artifact_root)
        parent = wb.save(task,PlanVersionCreateRequest.model_validate(content.hash_payload()),context_hash=context.record_hash,key="parent")
        diagnostic = summarize_dependency_failure("baseline_verify",1,"PinnedDownloadError: pinned ffmpeg unavailable") if native else summarize_dependency_failure("runtime_resolve",1,"No matching distribution found for timelib>=0.3.0")
        environment_hash = environment_inputs_hash(context,task_environment(wb,task))
        artifact = wb.artifact({"diagnostic":diagnostic})
        wb.append(task,"execution_environment_diagnostic",{"diagnostic":diagnostic,
            "environment_inputs_hash":environment_hash,"result_hash":artifact},key="failed-environment")
        assert task_preflight(wb,task)["issues"][0]["code"] == "environment_known_failure"
        request = PlanVersionCreateRequest.model_validate({**content.hash_payload(),"parent_version_id":parent.id,"goal":"Reworded goal"})
        child = wb.save(task,request,context_hash=context.record_hash,key="child")
        session.commit()
        counts = [session.scalar(select(func.count()).select_from(m)) for m in (Job,PlanApproval)]
        with pytest.raises(WorkbenchError,match="ffmpeg" if native else "timelib"):
            authorize_execution(wb,task,plan_id=child.id,plan_hash=child.record_hash,key="reject",settings=settings)
        assert counts == [session.scalar(select(func.count()).select_from(m)) for m in (Job,PlanApproval)]
        changed = wb.append(task,"context_ready",{**context.payload,"image":"sha256:"+"b"*64},key="changed-runner")
        assert environment_inputs_hash(changed,task_environment(wb,task)) != environment_hash
        assert task_preflight(wb,task)["status"] == "ready"
