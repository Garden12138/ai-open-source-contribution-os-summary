"""Admission and preparation must agree before a human authorizes execution."""
import asyncio
import hashlib
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api import create_app
from app.contribution_workflow import authorize_execution, ContributionStartWorker
from app.execution_preflight import python_project_error, task_preflight
from app.models import Job, PlanApproval, PlanLock, ExecutionAttempt
from app.planner import PlanningService
from app.schemas import PlanVersionCreateRequest
from app.workbench import WorkbenchError
from app.workbench_view import workbench_view
from tests.test_workbench import seeded, result, run_turn


def project_context(wb, task, context, text, *, large=False, missing=False):
    evidence = wb.read(context.payload["artifact_id"])
    evidence["inventory"].append("pyproject.toml")
    if not missing:
        evidence["entries"].append({"path":"pyproject.toml", "content":text, "executable":False,
                                    "prior_hash":hashlib.sha256(text.encode()).hexdigest()})
    if large:
        source = "# source text\n" * 55000
        entry = evidence["entries"][0]
        entry.update(content=source, prior_hash=hashlib.sha256(source.encode()).hexdigest())
    return wb.append(task,"context_ready",{**context.payload,"artifact_id":wb.artifact(evidence)},key="project-context")


@pytest.mark.parametrize("project,reason", [
    ({"name":"pkg","dependencies":[]},None),
    ({"name":"pkg","dynamic":["version"]},None),
    ({"name":"pkg","dynamic":["dependencies"]},"dynamic_dependencies"),
    ({"name":"pkg","dynamic":["optional-dependencies"]},"dynamic_dependencies"),
    ({},"project_metadata_missing"),
    ({"name":"pkg","dependencies":"pytest"},"project_metadata_invalid"),
    ({"name":"pkg","dependencies":["pkg @ https://untrusted.invalid/a.whl"]},"requirement_unsupported"),
    ({"name":"pkg","optional-dependencies":{"test":"pytest"}},"project_metadata_invalid"),
])
def test_shared_python_adapter_contract(project, reason):
    assert python_project_error({"project":project}) == reason


def test_dynamic_backend_admission_is_explicit_and_rejects_path_escape():
    from app.execution_preflight import inspect_environment
    metadata = {"project":{"name":"pkg","dynamic":["dependencies"]},
                "build-system":{"requires":["setuptools>=69"],"build-backend":"setuptools.build_meta"}}
    assert python_project_error(metadata) == "dynamic_dependencies"
    assert python_project_error(metadata,allow_dynamic=True) is None
    text = '[project]\nname="pkg"\ndynamic=["dependencies"]\n[build-system]\nrequires=["setuptools>=69"]\nbuild-backend="setuptools.build_meta"\n'
    selected = {"status":"supported","mode":"workspace-v2","ecosystem":"python","manifests":["pyproject.toml"]}
    admitted = inspect_environment(selected,{"entries":[{"path":"pyproject.toml","content":text,
        "prior_hash":hashlib.sha256(text.encode()).hexdigest()}]})
    assert admitted["status"] == "supported" and admitted["metadata_strategy"] == "wheel-metadata-v1"
    assert admitted["manifest_hashes"]["pyproject.toml"] == hashlib.sha256(text.encode()).hexdigest()
    bad = {**metadata,"build-system":{**metadata["build-system"],"backend-path":["../host"]}}
    assert python_project_error(bad,allow_dynamic=True) == "build_metadata_unsupported"


def test_large_coding_context_preserves_full_hash_and_exact_edit_and_legacy_validation():
    from app.coding import _parse_change_set_proposal
    from app.sandbox_worker.coding_context import CodingContext, CodingContextEntry
    from app.provenance import content_hash
    source = "# untouched source\n" * 18000 + 'def directory():\n    """will crete the file"""\n'
    entry = CodingContextEntry("salt/states/file.py",hashlib.sha256(source.encode()).hexdigest(),source,False)
    context = CodingContext.create(execution_attempt_id="attempt",plan_version_id="plan",
        plan_content_hash="1"*64,plan_record_hash="2"*64,base_commit_sha="a"*40,
        repository_archive_hash="3"*64,explore_result_hash="4"*64,entries=(entry,))
    assert context.version == "coding-context-v2"
    assert CodingContext.from_wire(context.to_wire()) == context
    proposal = json.dumps({"summary":"Fix typo","operations":[{"path":entry.path,"kind":"edit",
        "expected_prior_hash":entry.prior_hash,"content":None,
        "edits":[{"old_text":"will crete the file","new_text":"will create the file"}]}]})
    _, operations = _parse_change_set_proposal(proposal,context)
    assert operations[0].content == source.replace("will crete the file","will create the file")
    legacy = {**context.hash_payload(),"version":"coding-context-v1"}
    with pytest.raises(ValueError,match="Legacy"):
        CodingContext.from_wire({**legacy,"context_hash":content_hash(legacy)})
    small = CodingContext.create(**{k:v for k,v in context.hash_payload().items() if k not in {"version","entries"}},
        entries=(CodingContextEntry("small.py",hashlib.sha256(b"pass\n").hexdigest(),"pass\n",False),))
    legacy = {**small.hash_payload(),"version":"coding-context-v1"}
    assert CodingContext.from_wire({**legacy,"context_hash":content_hash(legacy)}).version == "coding-context-v1"


@pytest.mark.parametrize("case,expected", [("dynamic","environment_unsupported"),
    ("large","coding_file_budget_exceeded"), ("missing","environment_evidence_pending")])
def test_known_incompatibility_cannot_create_authorization_or_execution_jobs(tmp_path, case, expected):
    from app.conversation import conversation_view
    db, settings, task, content, context = seeded(tmp_path)
    settings = replace(settings, local_access_token="preflight-test")
    text = '[project]\nname="pkg"\n' + ('dynamic=["dependencies"]\n' if case == "dynamic" else 'dependencies=[]\n')
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        context = project_context(wb,task,context,text,large=case=="large",missing=case=="missing")
        plan = wb.save(task,PlanVersionCreateRequest.model_validate(content.hash_payload()),context_hash=context.record_hash,key="plan")
        session.commit()
        report = task_preflight(wb,task)
        assert report["status"] == "blocked"
        assert expected in {i["code"] for i in report["issues"]}
        assert not workbench_view(wb,task)["actions"]["can_execute"]
        assert conversation_view(wb,task)["pending_action"]["kind"] == "execution_blocked"
        from app.conversation import user_status
        from app.workbench import Workbench
        assert user_status(Workbench(session,"/no-artifact-access"),task)["label"] == "方案已保存，执行条件未满足"
        counts = [session.scalar(select(func.count()).select_from(model)) for model in (Job,PlanApproval,PlanLock,ExecutionAttempt)]
        with pytest.raises(WorkbenchError):
            authorize_execution(wb,task,plan_id=plan.id,plan_hash=plan.record_hash,key="direct",settings=settings)
    with TestClient(create_app(settings)) as client:
        path = f"/api/v1/tasks/{task}/workbench"
        assert client.get(path).json()["execution"]["preflight"]["report_hash"] == report["report_hash"]
        for key in ("first","second"):
            rejected = client.post(path+"/execute",json={"plan_id":plan.id,"plan_hash":plan.record_hash,
                "approve_plan":True,"start_execution":True,"max_repairs":2},
                headers={"X-ContribOS-Token":settings.local_access_token,"Idempotency-Key":key})
            assert rejected.status_code == 409, rejected.text
    with db.session() as session:
        wb = PlanningService(session,settings.artifact_root)
        assert counts == [session.scalar(select(func.count()).select_from(model)) for model in (Job,PlanApproval,PlanLock,ExecutionAttempt)]
        assert not wb.latest(task,"execution_authorized")
        assert wb.latest_plan(task).record_hash == plan.record_hash


def test_planning_reads_missing_environment_evidence_before_saving(tmp_path):
    db, settings, task, content, context = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session,settings.artifact_root)
        project_context(wb,task,context,"",missing=True)
        wb.request(task,text="准备方案",parent_id=None,expected_hash=None,key="plan")
    completed,_ = run_turn(db,settings,result(content))
    assert completed.state == "succeeded"
    with db.session() as session:
        wb = PlanningService(session,settings.artifact_root)
        assert not wb.latest_plan(task)
        next_job = session.get(Job,completed.result_data["next_job_id"])
        assert next_job.kind == "planning_context" and next_job.payload["paths"] == ["pyproject.toml"]
        assert not wb.latest(task,"execution_authorized")


def test_supported_authorization_binds_preflight_and_worker_rejects_changed_capability(tmp_path, monkeypatch):
    import app.execution_preflight
    db, settings, task, content, context = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session,settings.artifact_root)
        context = project_context(wb,task,context,'[project]\nname="pkg"\ndependencies=[]\n')
        plan = wb.save(task,PlanVersionCreateRequest.model_validate(content.hash_payload()),context_hash=context.record_hash,key="plan")
        session.commit()
        report = task_preflight(wb,task)
        assert report["status"] == "ready"
        job = authorize_execution(wb,task,plan_id=plan.id,plan_hash=plan.record_hash,key="execute",settings=settings)
        assert job.payload["preflight_hash"] == report["report_hash"]
    original = app.execution_preflight.task_preflight
    monkeypatch.setattr(app.execution_preflight,"task_preflight",lambda *args,**kwargs:{**original(*args,**kwargs),"report_hash":"0"*64})
    def no_network():
        pytest.fail("Stale capability must fail before contacting GitHub or executing")
    failed = asyncio.run(ContributionStartWorker(db,settings,no_network,worker_id="preflight").run_once())
    assert failed.state == "failed" and "执行能力" in failed.error_message
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(ExecutionAttempt)) == 0


def test_nonretryable_preparation_failure_reports_blocked_and_continue_does_not_restart(tmp_path):
    from app.contribution_workflow import advance_workflows
    from app.conversation import conversation_view, receive_message
    from app.jobs import JobService
    from tests.test_workbench import automatic_fixture
    db, settings, task, workers, code, _ = automatic_fixture(tmp_path)
    assert asyncio.run(workers[0].run_once()).state == "succeeded"  # Explore only.
    advance_workflows(db,settings)
    with db.session() as session:
        jobs = JobService(session)
        job = jobs.lease_next(worker_id="preparer",kinds=("execution_prepare",))
        jobs.start(job.id,worker_id="preparer")
        jobs.fail(job.id,worker_id="preparer",error_code="environment_manifest_unsupported",
                  error_message="动态依赖尚未适配；代码尚未修改。")
    advance_workflows(db,settings)
    with db.session() as session:
        wb = PlanningService(session,settings.artifact_root)
        blocker = wb.active_blocker(task)
        assert blocker.job_id == job.id and not blocker.payload["retryable"]
        assert blocker.payload["reason_code"] == "environment_manifest_unsupported"
        before = [(j.id,j.state) for j in wb.jobs(task)]
        plan_hash = wb.latest_plan(task).record_hash
        for key in ("resume-1","resume-2"):
            receive_message(wb,task,text="继续",key=key,settings=settings)
        assert before == [(j.id,j.state) for j in wb.jobs(task)]
        assert wb.latest_plan(task).record_hash == plan_hash
        view = conversation_view(wb,task)
        assert "受阻" in view["user_state"]["label"] and not view["user_state"]["busy"]
        assert view["pending_count"] == 0
        assert any("不能通过重复继续解决" in result["text"] for result in view["results"])
        assert not wb.latest(task,"automation_resumed")
    assert not code.invocations
