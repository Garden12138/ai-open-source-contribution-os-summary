"""A rejected read must not discard other reads, answers, usage or diagnostics."""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from app.archives import RepositoryArchiveStore
from app.database import Database
from app.planner import PlanningContextWorker, PlanningService
from app.planning_reads import planning_read_paths
from tests.test_workbench import seeded, run_turn, result

CANARY = "ghp_planningreadcanary123456789012345678"


def read_fixture(tmp_path):
    db, settings, task_id, content, context = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        evidence = wb.read(context.payload["artifact_id"])
        evidence["inventory"] += ["safe.py", "unavailable.py"]
        evidence["entries"].append({"path": "unavailable.py", "content": None,
                                     "prior_hash": None, "executable": None})
        wb.append(task_id, "context_ready", {
            **context.payload, "artifact_id": wb.artifact(evidence),
        }, key="read-context")
    return db, settings, task_id, content


def propose_reads(db, settings, task_id, *, questions=False, legacy=False):
    payload = {"reply": "需要补读代码。", "read_paths": [
        "safe.py", "missing.py", "unavailable.py", "safe.py"],
        "questions": [{"id": "scope", "prompt": "选择范围", "options": ["两个包", "一个包"]}]
        if questions else []}
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        if legacy:
            return wb.append(task_id, "assistant_message", payload, key="legacy", actor="planner")
        wb.request(task_id, text="准备方案", parent_id=None, expected_hash=None, key="start")
    completed, _ = run_turn(db, settings, json.dumps(payload))
    assert completed.state == "succeeded", completed.error_message
    return completed


@pytest.mark.parametrize("legacy", [False, True])
def test_clarification_answers_read_valid_subset_and_keep_feedback(tmp_path, legacy):
    db, settings, task_id, _ = read_fixture(tmp_path)
    propose_reads(db, settings, task_id, questions=True, legacy=legacy)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        before = [(e.id, e.record_hash) for e in wb.history(task_id)]
        job = wb.request(task_id, text="两个包", parent_id=None, expected_hash=None, key="answer")
        assert job.kind == "planning_context"
        assert job.payload["paths"] == ["safe.py"]
        assert job.payload["text"] == "两个包"
        assert job.payload["read_feedback"]["missing_paths"] == ["missing.py"]
        assert job.payload["read_feedback"]["unavailable_paths"] == ["unavailable.py"]
        assert wb.request(task_id, text="两个包", parent_id=None, expected_hash=None, key="answer").id == job.id
        assert [(e.id, e.record_hash) for e in wb.history(task_id)][:len(before)] == before
        assert not wb.latest(task_id, "execution_authorized")
    db.close()


def test_partial_read_then_full_plan_retains_feedback_usage_and_answers(tmp_path, monkeypatch):
    db, settings, task_id, content = read_fixture(tmp_path)
    first = propose_reads(db, settings, task_id)

    class Runtime:
        async def inspect(self, **kwargs):
            assert kwargs["paths"] == ("safe.py",)
            return {"inventory": [*content.files_to_inspect, *content.files_likely_to_change,
                                  "safe.py", "unavailable.py"], "truncated": False,
                    "entries": [{"path": "safe.py", "content": "safe code",
                        "prior_hash": hashlib.sha256(b"safe code").hexdigest(), "executable": False}]}

    monkeypatch.setattr(RepositoryArchiveStore, "lookup", lambda *a, **kw: object())
    read = asyncio.run(PlanningContextWorker(db, settings, Runtime(), worker_id="reader").run_once())
    assert read.state == "succeeded", read.error_message
    # Durable feedback and queue survive a process restart.
    db.close()
    db = Database(settings.database_url)
    final, runner = run_turn(db, settings, result(content))
    assert final.state == "succeeded", final.error_message
    prompt = json.loads(runner.invocations[0].prompt)
    assert prompt["read_feedback"]["missing_paths"] == ["missing.py"]
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest_plan(task_id)
        reads = [j for j in wb.jobs(task_id) if j.kind == "planning_context"]
        assert len(reads) == 1 and reads[0].payload["paths"] == ["safe.py"]
        receipt = next(e for e in wb.history(task_id) if e.kind == "assistant_message" and e.job_id == first.id)
        assert receipt.payload["usage"]["input_tokens"] > 0
        assert receipt.payload["read_paths"] == ["safe.py"]
        assert wb.latest(task_id, "model_output_rejected").payload["scope"] == "read_paths"
        assert not wb.latest(task_id, "execution_authorized")
    db.close()


def test_rejected_reads_have_bounded_repairs_and_cannot_save_accompanying_plan(tmp_path):
    db, settings, task_id, content = read_fixture(tmp_path)
    with db.session() as session:
        PlanningService(session, settings.artifact_root).request(task_id, text="准备方案",
            parent_id=None, expected_hash=None, key="invalid-only")
    reply = json.loads(result(content))
    reply["read_paths"] = ["missing.py", "unavailable.py", "../../outside", "/etc/passwd"]
    for _ in range(3):
        completed, runner = run_turn(db, settings, json.dumps(reply))
        assert completed.state == "succeeded", completed.error_message
        assert len(runner.invocations) == 1
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert len(wb.jobs(task_id)) == 3
        assert not wb.latest_plan(task_id)
        receipts = [e for e in wb.history(task_id) if e.kind == "assistant_message"]
        assert len(receipts) == 3 and all(e.payload["usage"]["input_tokens"] > 0 for e in receipts)
        assert all(e.payload["read_paths"] == [] for e in receipts)
        assert all(e.payload["read_feedback"]["unsafe_path_count"] == 2 for e in receipts)
        assert "上限" in receipts[-1].payload["reply"]
        assert not wb.latest(task_id, "execution_authorized")
        stored = "\n".join(session.connection().connection.driver_connection.iterdump())
        assert "../../outside" not in stored and "/etc/passwd" not in stored
    db.close()


def test_sensitive_read_path_is_rejected_without_repair_or_leak(tmp_path, caplog):
    db, settings, task_id, _ = read_fixture(tmp_path)
    with db.session() as session:
        PlanningService(session, settings.artifact_root).request(task_id, text="准备方案",
            parent_id=None, expected_hash=None, key="canary")
    completed, runner = run_turn(db, settings, json.dumps({"read_paths": [CANARY]}))
    assert completed.state == "failed" and completed.error_code == "planning_sensitive_output"
    assert len(runner.invocations) == 1
    with db.session() as session:
        stored = "\n".join(session.connection().connection.driver_connection.iterdump())
        assert CANARY not in stored
    assert CANARY not in caplog.text
    db.close()


@pytest.mark.parametrize("with_plan", [False, True])
def test_read_round_budget_persists_receipt_instead_of_rolling_back(tmp_path, with_plan, monkeypatch):
    db, settings, task_id, content = read_fixture(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        wb.request(task_id, text="准备方案", parent_id=None, expected_hash=None, key="limit")
    monkeypatch.setattr("app.planner.MAX_READ_ROUNDS", 0)
    reply = json.loads(result(content)) if with_plan else {"read_paths": ["safe.py"]}
    if with_plan:
        reply["plan"]["files_to_inspect"].append("safe.py")
    completed, _ = run_turn(db, settings, json.dumps(reply))
    assert completed.state == "succeeded", completed.error_message
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert len(wb.jobs(task_id)) == 1 and not wb.latest_plan(task_id)
        assert wb.latest(task_id, "assistant_message").payload["read_feedback"]["constraint"] == "read_round_limit"
    db.close()


def test_exact_paths_are_never_repaired_by_guessing():
    accepted, feedback = planning_read_paths(["src/typo.py"], {
        "inventory": ["src/type.py"], "entries": []})
    assert accepted == [] and feedback["missing_paths"] == ["src/typo.py"]
