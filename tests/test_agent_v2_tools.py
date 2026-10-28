"""Tool authority and actual credential-free Docker editing acceptance."""
import asyncio
import hashlib
import io
import os
import tarfile

import pytest

from app.agent_v2.contracts import Binding, Limits, TaskSpec, ToolCall
from app.agent_v2.tools import RestrictedToolRegistry
from app.archives import RepositoryArchiveStore
from app.provenance import content_hash
from app.sandbox_worker.agent_tools import DockerAgentToolGateway
from app.sandbox_worker.specs import SandboxPolicy
from app.workbench import WorkbenchError


def tool_spec(archive_hash="a" * 64, image="sha256:" + "a" * 64):
    return TaskSpec(binding=Binding(task_id="task", snapshot_id="snapshot", snapshot_hash="a"*64,
        attempt_id="attempt", attempt_hash="a"*64, plan_id="plan", plan_hash="b"*64,
        plan_content_hash="c"*64, authorization_hash="d"*64, base_sha="e"*40,
        archive_hash=archive_hash, image=image, policy_hash=SandboxPolicy().policy_hash,
        baseline_hash="f"*64, model_binding_hash="a"*64),
        goal="Correct addition without changing tests", write_paths=["calc.py"],
        commands=[{"command_id":"tests", "argv":["python3","-m","unittest","discover"], "working_directory":"."}])


def test_registry_rejects_scope_escape_and_command_injection():
    registry, spec = RestrictedToolRegistry(), tool_spec()
    for path in ("../a.py", "/etc/passwd", ".git/hooks/pre-commit", "a\\b"):
        with pytest.raises(ValueError):
            registry.validate(ToolCall(call_id="x", name="read_file", arguments={"path":path}), spec)
    with pytest.raises(WorkbenchError):
        registry.validate(ToolCall(call_id="x", name="apply_patch", arguments={"path":"tests.py", "expected_hash":None,
            "old_text":"", "new_text":"disable tests"}), spec)
    with pytest.raises(ValueError):
        registry.validate(ToolCall(call_id="x", name="run_approved_checks", arguments={"argv":["sh"]}), spec)


@pytest.mark.skipif(os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1", reason="actual v2 no-network sandbox tools")
@pytest.mark.parametrize("language", ["python", "typescript"])
def test_real_sandbox_incremental_patch_conflict_test_feedback_and_diff(tmp_path, language):
    prefix = "# 编码定位测试\n"
    source = prefix + "def add(a, b):\n    return a + b\n"
    tests = "import unittest\nfrom calc import add\nclass TestAdd(unittest.TestCase):\n    def test_sum(self): self.assertEqual(add(2, 3), 5)\n"
    source_path = "calc.py"
    test_path = "test_calc.py"
    argv = ["python3", "-m", "unittest", "discover"]
    if language == "typescript":
        source_path, test_path = "calc.ts", "test.mjs"
        prefix = "// 编码定位测试\n"
        source = prefix + "export const add = (a: number, b: number): number => a + b;\n"
        tests = "import {strict as assert} from 'node:assert';\nimport {add} from './calc.ts';\nassert.equal(add(2,3),5);\n"
        argv = ["node", "--experimental-strip-types", "test.mjs"]
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for p, data in {source_path:source, test_path:tests}.items():
            entry=tarfile.TarInfo("fixture-"+"e"*40+"/"+p)
            entry.size, entry.mode = len(data.encode()), 0o644
            bundle.addfile(entry, io.BytesIO(data.encode()))
        link=tarfile.TarInfo("fixture-"+"e"*40+"/linked-source")
        link.type,link.linkname=tarfile.SYMTYPE,source_path
        bundle.addfile(link)
    root = str(tmp_path / "artifacts")
    digest = RepositoryArchiveStore(root).put_file(archive)
    spec = tool_spec(digest, os.environ["CONTRIBOS_SANDBOX_RUNNER_IMAGE"])
    spec = spec.model_copy(update={"write_paths":[source_path], "commands":[{
        "command_id":"tests", "argv":argv, "working_directory":"."}]})
    env = {k: os.environ[k] for k in ("HOME","PATH","TMPDIR","DOCKER_CONTEXT","DOCKER_HOST") if k in os.environ}
    gateway = DockerAgentToolGateway(artifact_root=root, docker_environment=env,
        repository_for_attempt=lambda _: "fixture/fixture")

    async def scenario():
        snapshot, source_hash = None, digest
        receipts = []
        async def call(name, **arguments):
            nonlocal snapshot, source_hash
            c = ToolCall(call_id="call-"+str(len(receipts)), name=name, arguments=arguments)
            r = await gateway.execute(spec, c, RestrictedToolRegistry().validate(c,spec),
                source_hash=source_hash, snapshot=snapshot,
                input_hash=content_hash([c.model_dump(),source_hash]))
            receipts.append(r)
            snapshot = r.get("snapshot") or snapshot
            source_hash = r["result"]["output_source_hash"]
            return r["result"]
        baseline = await call("run_approved_checks")
        assert baseline["data"]["outcome"] == "passed"
        assert source_path in (await call("list_paths"))["data"]["text"]
        symlink = await call("read_file", path="linked-source")
        assert symlink["status"] == "rejected"
        assert symlink["failure"]["category"] == "security"
        file = (await call("read_file", path=source_path))["data"]
        assert file["text"] == source
        import json
        match=json.loads((await call("search_code",query="add"))["data"]["text"].splitlines()[0])
        assert match['path']==source_path and match['line']==2 and match['byte_offset']==len(prefix.encode())
        assert match['file_content_hash']==file['content_hash']
        located=(await call('read_file',path=source_path,offset=match['byte_offset'],expected_hash=match['file_content_hash']))['data']
        assert located['text']==source[len(prefix):]
        first = await call("apply_patch", path=source_path, expected_hash=file["content_hash"],
            old_text="a + b", new_text="a - b")
        assert first["status"] == "succeeded"
        wrong = await call("apply_patch", path=source_path, expected_hash=file["content_hash"],
            old_text="a - b", new_text="a + b")
        assert wrong["failure"]["safe_reason_code"] == "patch_hash_conflict"
        failed = await call("run_approved_checks")
        assert failed["data"]["outcome"] == "failed"
        assert "AssertionError" in failed["data"]["verification"]["command_evidence"][0]["stderr_log"]
        current = (await call("read_file", path=source_path))["data"]
        second = await call("apply_patch", path=source_path, expected_hash=current["content_hash"],
            old_text="a - b", new_text="a + b + 0")
        assert second["status"] == "succeeded"
        passed = await call("run_approved_checks")
        assert passed["data"]["outcome"] == "passed"
        diff = (await call("read_diff"))["data"]
        assert "a + b + 0" in diff["text"] and "--- a/" + source_path in diff["text"]
        assert diff["next_offset"] is None
        assert snapshot["files"][source_path]["original_content"] == source
        assert len([r for r in receipts if r["result"]["data"].get("tool") == "apply_patch"]) == 3
    asyncio.run(scenario())


@pytest.mark.skipif(os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1", reason="actual help-only verifier evidence")
@pytest.mark.parametrize('mode',['help','zero_tests'])
def test_real_sandbox_help_only_zero_exit_never_passes(tmp_path,mode):
    root=str(tmp_path/'artifacts');archive=tmp_path/'source.tar.gz'
    with tarfile.open(archive,'w:gz') as bundle:
        entry=tarfile.TarInfo('fixture-'+'e'*40+'/calc.py');entry.size=4;entry.mode=0o644
        bundle.addfile(entry,io.BytesIO(b'pass'))
    digest=RepositoryArchiveStore(root).put_file(archive)
    spec=tool_spec(digest,os.environ['CONTRIBOS_SANDBOX_RUNNER_IMAGE']).model_copy(update={'commands':[{
        'command_id':'check','argv':['python3','-c','print("Usage: check [options]\\nFlags:\\n  --help")'] if mode=='help' else ['python3','-m','unittest','discover'],'working_directory':'.'}]})
    env={k:os.environ[k] for k in ('HOME','PATH','TMPDIR','DOCKER_HOST','DOCKER_CONTEXT') if k in os.environ}
    gateway=DockerAgentToolGateway(artifact_root=root,docker_environment=env,repository_for_attempt=lambda _:'fixture/fixture')
    call=ToolCall(call_id='help-probe',name='run_approved_checks',arguments={})
    result=asyncio.run(gateway.execute(spec,call,RestrictedToolRegistry().validate(call,spec),
        source_hash=digest,snapshot=None,input_hash=content_hash(call.model_dump())))['result']
    assert result['status']=='rejected' and result['failure']['category']=='environment'
    assert result['data']['verification']['command_evidence'][0]['exit_code']==0
    assert result['data']['outcome']=='environment_blocked'


@pytest.mark.skipif(os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1", reason="actual failed-baseline boundary")
def test_real_baseline_failure_never_enters_coding_or_expands_scope(tmp_path,monkeypatch):
    from app.agent_v2.orchestrator import TaskOrchestrator
    from app.contribution_workflow import advance_workflows
    from app.models import ExecutionAttempt
    from app.plans import PlanCommand
    from app.workbench import Workbench
    from sqlalchemy import select
    from tests.test_node_workflow import node_workflow, cleanup_node_workflow
    files={'calc.py':'def add(a,b):\n    return a-b\n',
        'tests/test_calc.py':'import unittest\nfrom calc import add\nclass Tests(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(2,3),5)\n'}
    db,s,task,workers,code,_=node_workflow(tmp_path,monkeypatch,'npm','10.9.4',real=True,
        files_override=files,source_override='calc.py',runtime_v2=True,
        commands_override=(PlanCommand('tests','Run approved tests',('python3','-m','unittest','discover','-s','tests')),))
    try:
        assert asyncio.run(workers[0].run_once()).state=='succeeded'
        advance_workflows(db,s)
        assert asyncio.run(workers[-1].run_once()).state=='succeeded'
        with db.session() as session:
            wb=Workbench(session,s.artifact_root)
            baseline=wb.read(wb.latest(task,'execution_baseline_ready').payload['result_hash'])
            assert baseline['outcome']['status']=='failed'
            attempt=session.scalar(select(ExecutionAttempt).where(ExecutionAttempt.task_id==task))
            with pytest.raises(WorkbenchError) as exc:TaskOrchestrator(wb,s).start(attempt.id)
            assert exc.value.code=='agent_baseline_blocked'
            assert not wb.latest(task,'agent_v2_session') and not code.invocations
            assert wb.latest_plan(task).files_likely_to_change==['calc.py']
    finally:
        asyncio.run(cleanup_node_workflow(db,s,workers));db.close()


def test_artifact_pages_roundtrip_utf8_and_never_accept_partial_character_offset():
    from app.agent_v2.tools import artifact_page
    raw=('可验证证据🙂'*200).encode();offset=0;parts=[]
    while True:
        page=artifact_page(raw,offset,129);parts.append(page['text'])
        if page['next_offset'] is None:break
        assert page['next_offset']>offset;offset=page['next_offset']
    assert ''.join(parts).encode()==raw
    with pytest.raises(WorkbenchError):artifact_page(raw,1,129)
    with pytest.raises(WorkbenchError):artifact_page(raw,len(raw)+1,129)
