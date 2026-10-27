"""Project-derived preparation, rather than repository-specific templates."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace

import pytest

from app.node_environments import node_environment_plan, inspect_node_environment, validate_node_recipe
from tests.test_node_environments import node_files, dependency_plan


def bun_files(*, locked=True):
    files = node_files("npm", "10.9.4")
    files.pop("package-lock.json")
    package = json.loads(files["package.json"])
    package["packageManager"] = "bun@>=1.4"
    package["dependencies"]["@fixture/lib"] = "workspace:*"
    package["scripts"]["prepare"] = "bun prepare.cjs"
    files["package.json"] = json.dumps(package)
    if locked:
        files["bun.lock"] = json.dumps({"lockfileVersion":1,"configVersion":1,
            "workspaces":{"":{"name":"environment-fixture","dependencies":{"@fixture/lib":"workspace:*"}},
                          "packages/lib":{"name":"@fixture/lib","version":"1.0.0"}},
            "packages":{"@fixture/lib":["@fixture/lib@workspace:packages/lib"]}})
    return files


def resolve(files):
    selection = node_environment_plan(set(files), ["packages/lib/index.js"])
    return inspect_node_environment(selection, {"inventory":sorted(files), "entries":[
        {"path":p,"content":text,"prior_hash":hashlib.sha256(text.encode()).hexdigest()} for p,text in files.items()]})


def test_bun_range_and_ancestor_lock_select_project_toolchain():
    files = bun_files()
    value = resolve(files)
    assert value["status"] == "supported", value
    recipe = value["node_environment"]
    assert recipe["version"] == "node-environment-v2"
    assert recipe["manager"] == "bun" and recipe["manager_constraint"] == ">=1.4"
    assert recipe["lockfile"] == "bun.lock" and recipe["project_directory"] == "."
    assert dependency_plan(files).node_environment == recipe


def test_project_without_lock_resolves_only_in_private_preparation_copy():
    files = node_files("npm","10.9.4")
    del files["package-lock.json"]
    # A project without an ancestor lock needs the root workspace declaration.
    selection = node_environment_plan(set(files), ["package.json"])
    evidence = {"entries":[{"path":p,"content":text,"prior_hash":hashlib.sha256(text.encode()).hexdigest()} for p,text in files.items()]}
    value = inspect_node_environment(selection,evidence)
    assert value["status"] == "supported", value
    assert value["node_environment"]["lock_strategy"] == "resolve_private"
    assert value["node_environment"]["lockfile"] is None
    assert "package-lock.json" not in files


def test_verification_uses_actual_project_script_and_never_installs():
    from app.plans import PlanCommand
    from app.verification_commands import verification_command_error
    files=bun_files()
    evidence={"entries":[{"path":p,"content":v} for p,v in files.items()]}
    command=PlanCommand("types","Check types",("bun","run","typecheck"))
    assert verification_command_error([command],set(files),set(),evidence)["constraint"]=="project_script_missing"
    assert verification_command_error([replace(command,argv=("bun","run","test"))],set(files),set(),evidence) is None
    assert verification_command_error([replace(command,argv=("bun","install"))],set(files),set(),evidence)["constraint"]=="dependency_preparation_separate"


@pytest.mark.parametrize("argv", [("yarn","--cwd","packages/lib","run"),
    ("npm","--prefix","packages/lib","run"), ("pnpm","-C","packages/lib","run")])
def test_script_checks_follow_the_actual_package_directory(argv):
    from app.plans import PlanCommand
    from app.verification_commands import verification_command_error
    evidence = {"entries":[{"path":"packages/lib/package.json","content":'{"scripts":{"check:types":"tsc --noEmit"}}'}]}
    inventory = {"package.json", "packages/lib/package.json"}
    invalid = PlanCommand("types", "Check actual package types", (*argv, "typecheck"))
    error = verification_command_error([invalid], inventory, set(), evidence)
    assert error["constraint"] == "project_script_missing"
    assert error["paths"] == ["packages/lib/package.json"]
    assert verification_command_error([replace(invalid, argv=(*argv, "check:types"))], inventory, set(), evidence) is None


def test_inventory_search_queries_full_inventory_with_bounded_pages(tmp_path):
    from tests.test_agent_runtime import runtime_fixture
    from app.workbench import Workbench
    from app.conversation_reasoning import EvidenceRead,read_evidence
    db,s,task,*_=runtime_fixture(tmp_path)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        context=w.latest(task,"context_ready")
        value=w.read(context.payload["artifact_id"])
        value["inventory"] += ["bun.lock","bunfig.toml"] + [f"examples/{i}/package.json" for i in range(100)]
        w.append(task,"context_ready",{**context.payload,"artifact_id":w.artifact(value)},key="search-inventory")
        result=read_evidence(w,task,EvidenceRead(name="repository_file",query="LOCK"))
        assert [p["path"] for p in result["data"]["files"]]==["bun.lock"]
        assert result["data"]["files"][0]["read"] is False
        page=read_evidence(w,task,EvidenceRead(name="repository_file",query="package.json"))["data"]
        assert len(page["files"])==40 and page["next_offset"]==40 and page["matches"]==100


def test_environment_facts_expose_real_manager_and_locks_without_claiming_preparation(tmp_path):
    from tests.test_agent_runtime import runtime_fixture
    from app.workbench import Workbench
    from app.conversation_reasoning import EvidenceRead,read_evidence
    db,s,task,*_=runtime_fixture(tmp_path)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        context=w.latest(task,"context_ready")
        files=bun_files()
        artifact=w.artifact({"inventory":sorted(files),"entries":[{"path":p,"content":text,
            "prior_hash":hashlib.sha256(text.encode()).hexdigest(),"executable":False} for p,text in files.items()],"truncated":False})
        receipt=w.append(task,"context_ready",{**context.payload,"artifact_id":artifact},key="environment-facts")
        value=read_evidence(w,task,EvidenceRead(name="environment"))
        assert value["data"]["declared_manager"]=="bun@>=1.4"
        assert value["data"]["lockfiles"]==["bun.lock"]
        assert value["data"]["baseline_available"] is False
        assert value["reference"]["source_hash"]==receipt.record_hash


def test_resuming_failed_read_inherits_readonly_scope_in_prompt_and_schema(tmp_path):
    from tests.test_agent_runtime import runtime_fixture,send
    from tests.test_conversation_reasoning import run
    from app.conversation import advance_conversations
    from app.workbench import Workbench
    from app.agent_runtime import turn_effect
    db,s,task,*_=runtime_fixture(tmp_path)
    send(db,s,task,"解释已经读取的源码")
    advance_conversations(db,s)
    failed,_=run(db,s,{"effect":"observe","reads":[{"name":"repository_file","path":"app/plans.py"}]},"invalid","invalid")
    assert failed.state=="failed"
    advance_conversations(db,s)
    resumed=send(db,s,task,"继续",key="resume-read")
    with db.session() as session:
        assert turn_effect(Workbench(session,s.artifact_root),task,resumed["message_id"])=="observe"
    job,runner=run(db,s,{"effect":"observe","reply":"继续处理原来的源码解释；已有证据保留。"})
    assert job.state=="succeeded",job.error_message
    prompt=json.loads(runner.invocations[0].prompt)
    assert prompt["message"]=="解释已经读取的源码"
    assert prompt["resume_context"]["control_text"]=="继续"
    assert prompt["observation"]["available_actions"]==["respond"]
    assert runner.invocations[0].output_schema["properties"]["action"]["enum"]==["respond"]
    assert runner.invocations[0].output_schema["properties"]["effect"]["const"]=="observe"


def test_long_recent_assistant_reply_does_not_pin_the_whole_next_request(tmp_path):
    from tests.test_agent_runtime import runtime_fixture
    from app.workbench import Workbench
    from app.agent_context import admit_prompt,input_upper_bound
    db,s,task,*_=runtime_fixture(tmp_path)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        job=w.enqueue(task,"conversation_intent",{},key="large-previous-reply")
        prompt=json.dumps({"message":"检查锁文件", "rules":["Preserve scope"],
            "recent_dialogue":[{"role":"assistant","text":"已保存的长方案"*4000},{"role":"user","text":"检查锁文件"}]})
        wire,budget=admit_prompt(w,task,prompt,{},job_id=job.id)
        assert budget["admitted"] and input_upper_bound(wire,{})<=16000
        assert json.loads(wire)["message"]=="检查锁文件"


def test_three_evidence_results_share_the_wire_budget_without_losing_originals(tmp_path):
    from tests.test_agent_runtime import runtime_fixture,send
    from app.workbench import Workbench
    from app.agent_context import admit_prompt,input_upper_bound
    from app.conversation_reasoning import reasoning_prompt
    from app.conversation_protocol import step_schema,RUNTIME_REASONING_VERSION
    db,s,task,*_=runtime_fixture(tmp_path)
    source=send(db,s,task,"先检查是否有锁文件")
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        job=w.enqueue(task,"conversation_intent",{},key="three-evidence-results")
        files={"files":[{"path":f"examples/{i}/package.json","read":False} for i in range(200)]+[{"path":"bun.lock","read":False}],"query":"lock","matches":201}
        plan={"goal":"Preserve the issue scope and use the actual Bun project scripts.","acceptance_criteria":["existing requirement "*500]}
        originals=[files,files|{"query":"package.json"},plan]
        results=[{"reference":{"name":"repository_file" if i<2 else "plan","available":True},"data":data} for i,data in enumerate(originals)]
        prompt,view=reasoning_prompt(w,task,source["message_id"],results,None,3,job_id=job.id,reasoning_version=RUNTIME_REASONING_VERSION)
        schema=step_schema(view["available_actions"],{"repository_file","plan"},allow_reads=True,version=RUNTIME_REASONING_VERSION)
        wire,budget=admit_prompt(w,task,prompt,schema,job_id=job.id)
        body=json.loads(wire)
        assert budget["admitted"] and input_upper_bound(wire,schema)<=16000
        assert body["message"]=="先检查是否有锁文件"
        for result,original in zip(body["tool_results"],originals):
            assert result["data"]["partial"] and w.read(result["data"]["context_reference"])["data"]==original
        assert any(f["path"]=="bun.lock" for f in body["tool_results"][0]["data"]["files"])


@pytest.mark.parametrize("constraint",[">=1.4", "^1.4.0", "~1.4.0", ">=1.4 <2"])
def test_version_requirements_stay_bound_until_isolated_resolution(constraint):
    files=bun_files()
    package=json.loads(files["package.json"])
    package["packageManager"]="bun@"+constraint
    files["package.json"]=json.dumps(package)
    value=resolve(files)
    assert value["status"]=="supported",value
    assert value["node_environment"]["manager_constraint"]==constraint


def test_ci_and_toolchain_file_constraints_come_from_frozen_project_data():
    files=bun_files()
    files[".github/workflows/test.yml"]="jobs:\n  test:\n    steps:\n      - uses: oven-sh/setup-bun@v2\n        with:\n          bun-version: '1.4.4'\n"
    files[".tool-versions"]="bun 1.4.4\n"
    value=resolve(files)
    assert value["status"]=="supported",value
    assert value["node_environment"]["manager_constraint"]==">=1.4 1.4.4"
    assert ".github/workflows/test.yml" in value["manifest_hashes"]
    assert dependency_plan(files).node_environment==value["node_environment"]


def test_unknown_toolchain_is_an_explicit_capability_gap_not_missing_lock():
    files=bun_files()
    root=json.loads(files["package.json"])
    root["packageManager"]="unavailable-manager@1.2.3"
    files["package.json"]=json.dumps(root)
    value=resolve(files)
    assert value["reason_code"]=="node_manager_capability_missing"
    assert "不是缺少锁文件" in value["reason"]


def test_manager_constraint_failure_has_a_toolchain_diagnostic():
    from app.sandbox_worker.dependency_diagnostics import summarize_dependency_failure,validate_dependency_diagnostic
    value=summarize_dependency_failure("node_tools",1,"No matching toolchain version")
    assert value["reason_code"]=="toolchain_version_unavailable"
    assert validate_dependency_diagnostic(value)==value


@pytest.mark.parametrize("lock,strategy",[(None,"existing"),("bun.lock","resolve_private"),("../bun.lock","existing"),("yarn.lock","existing")])
def test_signed_resolution_rejects_inconsistent_lock_binding(lock,strategy):
    recipe=resolve(bun_files())["node_environment"]
    with pytest.raises(ValueError):
        validate_node_recipe({**recipe,"lockfile":lock,"lock_strategy":strategy})


@pytest.mark.skipif(os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE")!="1", reason="real credential-free OCI preparation")
@pytest.mark.parametrize("locked",[True,False])
def test_real_bun_preparation_baseline_and_changed_source(tmp_path,monkeypatch,locked):
    from tests import test_node_environments as fixtures
    files=bun_files(locked=locked)
    monkeypatch.setattr(fixtures,"node_files",lambda *args:files)
    fixtures.test_real_node_workspace_baseline_implementation_and_fresh_verify(tmp_path,"bun",">=1.4")


@pytest.mark.skipif(os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE")!="1", reason="real credential-free OCI preparation")
def test_real_npm_without_source_lock_freezes_private_dependencies(tmp_path,monkeypatch):
    from tests import test_node_environments as fixtures
    files=node_files("npm","10.9.4")
    del files["package-lock.json"]
    monkeypatch.setattr(fixtures,"node_files",lambda *args:files)
    fixtures.test_real_node_workspace_baseline_implementation_and_fresh_verify(tmp_path,"npm","10.9.4")
