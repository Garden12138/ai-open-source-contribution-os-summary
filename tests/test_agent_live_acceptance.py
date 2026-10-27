"""Opt-in real-model + real-OCI acceptance over controlled repository archives.

GitHub discovery and the already approved plan are deterministic fixtures. Code
and independent review responses are exclusively from the configured provider;
all repository execution happens in the production Docker sandbox. These checks
are not a replacement for dedicated GitHub repository/account acceptance.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from app.agent_runtime import activate
from app.contribution_workflow import advance_workflows
from app.execution_acceptance import acceptance_view, accept_execution
from app.models import AgentInvocation, CodingTurn, Job, ReviewRun
from app.providers.contracts import ProviderIdentity
from app.providers.nvidia_nim import NvidiaNimCompletion
from app.provenance import content_hash
from app.workbench import Workbench
from tests.test_node_workflow import cleanup_node_workflow, node_workflow

pytestmark = pytest.mark.skipif(os.getenv("CONTRIBOS_RUN_AGENT_LIVE_ACCEPTANCE") != "1",
    reason="opt-in paid configured-model and real isolated Docker acceptance")

ROOT = Path(__file__).resolve().parents[1]
PROBE = (ROOT / "scripts/agent_model_probe.py").read_text()


async def provider_request(request: dict) -> dict:
    process = await asyncio.create_subprocess_exec("docker", "compose", "--env-file", ".env",
        "-f", "docs/deployment/compose.yaml", "exec", "-T", "provider-worker", "python", "-c", PROBE,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=ROOT)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(json.dumps(request).encode()), 240)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise AssertionError("Acceptance provider timed out") from None
    assert process.returncode == 0, "Acceptance provider rejected request (raw provider errors suppressed)"
    assert len(stdout) < 2_000_000
    return json.loads(stdout)


class RemoteRunner:
    def __init__(self, stage: str, identity: ProviderIdentity):
        self.stage, self.identity = stage, identity
        self.invocations = []

    async def complete(self, invocation):
        self.invocations.append(invocation)
        wire = asdict(invocation)
        wire["stage"] = invocation.stage.value
        response = await provider_request({"stage":self.stage,
            "expected_identity":self.identity.hash_payload(), "invocation":wire})
        assert response["identity"] == self.identity.hash_payload()
        return NvidiaNimCompletion(**response["completion"])


def python_files(dynamic: bool) -> dict[str, str]:
    project = ('[build-system]\nrequires=["setuptools>=69"]\nbuild-backend="setuptools.build_meta"\n'
        '[project]\nname="contribos-agent-acceptance"\nversion="0.1.0"\n'
        'dependencies=["workspace-common>=0.1.0"]\n[tool.setuptools]\npackage-dir={""="src"}\npy-modules=["workspace_demo"]\n')
    files = {
        "python/pkg/pyproject.toml":project,
        "python/pkg/src/workspace_demo.py":"from workspace_common import identity\n\ndef add(a, b):\n    return identity(a - b)\n",
        "python/pkg/tests/test_demo.py":"import unittest\nfrom workspace_demo import add\n\nclass AdditionTest(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
        "python/workspace-common/pyproject.toml":project.replace('contribos-agent-acceptance','workspace-common').replace('dependencies=["workspace-common>=0.1.0"]','').replace('workspace_demo','workspace_common'),
        "python/workspace-common/src/workspace_common.py":"def identity(value):\n    return value\n",
    }
    if dynamic:
        files["python/pkg/pyproject.toml"] = project.replace('dependencies=["workspace-common>=0.1.0"]',
            'dynamic=["dependencies"]') + '\n[tool.setuptools.dynamic]\ndependencies={file=["requirements-dynamic.txt"]}\n'
        files["python/pkg/requirements-dynamic.txt"] = "workspace-common>=0.1.0\npackaging>=24; python_version >= '3.8'\n"
    return files


@pytest.mark.parametrize("sample", ["npm-typescript", "yarn-classic", "yarn-modern", "pnpm", "python-declarative", "python-dynamic"])
def test_configured_model_and_real_sandbox_complete_without_operator_patch(tmp_path, monkeypatch, sample):
    from tests import test_workbench as fixtures
    from app import model_settings
    metadata = asyncio.run(provider_request({"operation":"metadata"}))
    identities = {stage:ProviderIdentity(**item["identity"]) for stage,item in metadata.items()}
    # Bind the fixture approval to the actual configured provider identities.
    monkeypatch.setattr(model_settings, "task_identity", lambda session, settings, task, stage:identities[stage])
    seeded = fixtures.seeded
    def runtime_seed(path):
        result = seeded(path)
        with result[0].session() as session:
            activate(Workbench(session, result[1].artifact_root), result[2])
            session.commit()
        return result
    monkeypatch.setattr(fixtures, "seeded", runtime_seed)
    options = {}
    managers = {"npm-typescript":("npm","10.9.4"), "yarn-classic":("yarn","1.22.22"),
                "yarn-modern":("yarn","4.9.2"), "pnpm":("pnpm","10.11.0")}
    if sample.startswith("python"):
        from app.plans import PlanCommand
        files = python_files(sample == "python-dynamic")
        manager, version = "npm", "10.9.4"  # unused by the Python environment resolver
        options = {"files_override":files, "source_override":"python/pkg/src/workspace_demo.py",
            "commands_override":(PlanCommand("addition-test", "Verify installed project addition behavior",
                ("python", "-m", "unittest", "discover", "-s", "tests"), working_directory="python/pkg"),),
            "goal_override":"Fix add(a,b) to return their sum via identity. Preserve the existing public interface, local package dependency and tests. Modify only python/pkg/src/workspace_demo.py."}
    else:
        manager, version = managers[sample]
        options["goal_override"] = "Change the exported value in the library from 'before' to 'after'. Preserve workspace dependencies, build hooks and all existing tests. Modify only the library source."
        if sample == "npm-typescript":
            from tests.test_node_environments import node_files
            files = {p.replace("index.js","index.ts"):text.replace("index.js","index.ts")
                     for p,text in node_files(manager,version).items()}
            files["packages/lib/index.ts"] = "export const value: string = 'before';\n"
            files["package.json"] = files["package.json"].replace("node test.mjs", "node --experimental-strip-types test.mjs")
            options.update(files_override=files, source_override="packages/lib/index.ts")
    db, settings, task, workers, _, _ = node_workflow(tmp_path, monkeypatch, manager, version, real=True, **options)
    code = RemoteRunner("implementation", identities["implementation"])
    review = RemoteRunner("review", identities["review"])
    workers[2].runner, workers[2].identity = code, code.identity
    workers[3].runner, workers[3].identity = review, review.identity
    try:
        ready = None
        for _ in range(30):
            for worker in workers:
                job = asyncio.run(worker.run_once())
                if job:
                    assert job.state in {"succeeded","queued"}, (sample, job.kind, job.error_code, job.error_message)
            advance_workflows(db, settings)
            with db.session() as session:
                wb = Workbench(session, settings.artifact_root)
                ready = wb.latest(task, "awaiting_acceptance")
                assert not wb.active_blocker(task), (sample, wb.active_blocker(task).payload if wb.active_blocker(task) else None)
                if ready:
                    review_row = session.scalar(select(ReviewRun).order_by(ReviewRun.created_at.desc()))
                    assert review_row.verdict == "pass"
                    assert not list(session.scalars(select(CodingTurn)))
                    assert not wb.latest(task, "agent_maintenance_recorded")
                    view = acceptance_view(wb, task)
                    assert view["available"] and not view["accepted"]
                    accept_execution(wb, task, binding_hash=view["binding_hash"], text="Controlled acceptance result approved", key="live-human")
                    assert not wb.latest(task, "publication_confirmed")
                    invocations = list(session.scalars(select(AgentInvocation)))
                    report = {"sample":sample, "task_id":task,
                        "classification":"controlled_archive_real_model_real_docker",
                        "discovery":"fixture", "plan":"approved_fixture", "external_writes":0,
                        "code_model":code.identity.hash_payload(), "review_model":review.identity.hash_payload(),
                        "code_calls":len(code.invocations), "review_calls":len(review.invocations),
                        "input_tokens":sum(i.input_tokens for i in invocations),
                        "output_tokens":sum(i.output_tokens for i in invocations),
                        "review_hash":review_row.record_hash, "acceptance_hash":view["binding_hash"],
                        "maintenance_records":0, "runner_image":settings.workbench_runner_image}
                    report["report_hash"] = content_hash(report)
                    (tmp_path / "acceptance.json").write_text(json.dumps(report,indent=2))
                    break
        assert ready, (sample, "did not reach acceptance")
        assert code.invocations and review.invocations
    finally:
        asyncio.run(cleanup_node_workflow(db, settings, workers))
        db.close()


def test_live_conversation_answers_compound_question_while_background_continues(tmp_path):
    from app.conversation import ConversationIntentWorker, advance_conversations, conversation_view
    from tests.test_agent_runtime import runtime_fixture, send
    db, settings, task, *_ = runtime_fixture(tmp_path)
    identity = ProviderIdentity(**asyncio.run(provider_request({"operation":"metadata"}))["planning"]["identity"])
    runner = RemoteRunner("planning", identity)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        source = wb.latest(task, "context_ready")
        evidence = wb.read(source.payload["artifact_id"])
        import hashlib
        content = "def add(a, b):\n    return a + b\n"
        for entry in evidence["entries"]:
            if entry["path"] == "app/plans.py":
                entry.update(content=content, prior_hash=hashlib.sha256(content.encode()).hexdigest())
        wb.append(task, "context_ready", {**source.payload,"artifact_id":wb.artifact(evidence)}, key="live-source")
        background = wb.enqueue(task, "test_background", {}, key="keep-running")
        background_id = background.id
    send(db, settings, task, "看一下现在的进度，并读取 app/plans.py 的实际内容解释。只回答问题，现有工作继续，不要修改或停止。")
    advance_conversations(db, settings)
    job = asyncio.run(ConversationIntentWorker(db, settings, runner, identity, worker_id="live-dialogue").run_once())
    assert job.state == "succeeded", (job.error_code,job.error_message)
    advance_conversations(db, settings)
    with db.session() as session:
        w = Workbench(session, settings.artifact_root)
        view = conversation_view(w, task)
        assert view["blocking_count"] == 0
        assert w.session.get(Job,background_id).state == "queued"
        assert not w.latest(task, "stop_requested") and not w.latest(task, "execution_authorized")
        reads = [e for e in w.history(task) if e.kind == "conversation_tool_result"
                 and e.payload.get("tool",{}).get("path") == "app/plans.py"]
        assert reads and w.read(reads[-1].payload["artifact_id"])["data"]["partial"] is False
        report = {"classification":"real_model_readonly_dialogue_controlled_task", "model":identity.hash_payload(),
            "calls":len(runner.invocations), "background_unchanged":True, "source_read":True,
            "answer":view["results"][-1]["text"], "external_writes":0}
        (tmp_path / "dialogue-acceptance.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
    assert 1 <= len(runner.invocations) <= 4
    assert any(json.loads(i.prompt).get("tool_results") for i in runner.invocations)
    db.close()
