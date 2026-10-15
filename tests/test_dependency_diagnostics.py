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


def test_failed_environment_receipt_blocks_same_inputs_across_plan_edits(tmp_path):
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
        diagnostic = summarize_dependency_failure("runtime_resolve",1,"No matching distribution found for timelib>=0.3.0")
        environment_hash = environment_inputs_hash(context,task_environment(wb,task))
        artifact = wb.artifact({"diagnostic":diagnostic})
        wb.append(task,"execution_environment_diagnostic",{"diagnostic":diagnostic,
            "environment_inputs_hash":environment_hash,"result_hash":artifact},key="failed-environment")
        assert task_preflight(wb,task)["issues"][0]["code"] == "environment_known_failure"
        request = PlanVersionCreateRequest.model_validate({**content.hash_payload(),"parent_version_id":parent.id,"goal":"Reworded goal"})
        child = wb.save(task,request,context_hash=context.record_hash,key="child")
        session.commit()
        counts = [session.scalar(select(func.count()).select_from(m)) for m in (Job,PlanApproval)]
        with pytest.raises(WorkbenchError,match="timelib"):
            authorize_execution(wb,task,plan_id=child.id,plan_hash=child.record_hash,key="reject",settings=settings)
        assert counts == [session.scalar(select(func.count()).select_from(m)) for m in (Job,PlanApproval)]
        changed = wb.append(task,"context_ready",{**context.payload,"image":"sha256:"+"b"*64},key="changed-runner")
        assert environment_inputs_hash(changed,task_environment(wb,task)) != environment_hash
        assert task_preflight(wb,task)["status"] == "ready"
