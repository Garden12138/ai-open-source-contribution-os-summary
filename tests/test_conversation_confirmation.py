"""Chat confirmation explains exact admission and never grants execution."""
from dataclasses import replace
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import create_app
from app.conversation import (advance_conversations, conversation_view,
                              plan_confirmation_requested, receive_message)
from app.conversation_reasoning import observation
from app.execution_environment import environment_plan
from app.models import ExecutionAttempt, PlanApproval
from app.planner import PlanningService
from app.plans import PlanCommand
from app.schemas import PlanVersionCreateRequest
from tests.test_conversation_reasoning import request, run
from tests.test_workbench import seeded


def saved_plan(tmp_path, *, lock=None):
    db, settings, task, content, context = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        if lock:
            target = "excalidraw-app/data/tabSync.ts"
            evidence = wb.read(context.payload["artifact_id"])
            evidence["inventory"] += ["package.json", lock, "excalidraw-app/package.json", target]
            evidence["entries"].append({"path": target, "content": "console.error('verison');",
                "prior_hash": "a" * 64, "executable": False})
            context = wb.append(task, "context_ready", {**context.payload,
                "artifact_id": wb.artifact(evidence)}, key="node-context")
            content = replace(content, files_to_inspect=(target,), files_likely_to_change=(target,),
                commands_to_run=(PlanCommand(command_id="typecheck", purpose="Check types",
                                            argv=("yarn", "test:typecheck")),))
        plan = wb.save(task, PlanVersionCreateRequest.model_validate(content.hash_payload()),
                       context_hash=context.record_hash, key="saved-plan")
        session.commit()
    return db, settings, task, plan


@pytest.mark.parametrize("text", ["确认方案", "确定方案。", "请确认当前方案", "同意这个方案！", "确认方案并开始"])
def test_exact_confirmation_phrase_is_only_a_readonly_request(text):
    assert plan_confirmation_requested(text)


@pytest.mark.parametrize("text", ["好", "确认", "不要确认方案", "确认方案？", "确认方案后会怎样？",
                                  "确认方案，但先增加测试", "那你准备node环境"])
def test_questions_and_new_requirements_are_not_bare_confirmations(text):
    assert not plan_confirmation_requested(text)


@pytest.mark.parametrize("lock", [None, "yarn.lock", "pnpm-lock.yaml"])
def test_late_chat_confirmation_keeps_plan_and_explains_actual_blockers_without_jobs(tmp_path, lock):
    db, settings, task, plan = saved_plan(tmp_path, lock=lock)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        wb.append(task, "conversation_result", {"version": "conversation-v1",
            "text": "此前回复错误地声称确认后会自动准备环境。"}, key="old-misleading-reply")
        original = [(e.id, e.record_hash) for e in wb.history(task)]
        receipt = receive_message(wb, task, text="确认方案", key="late-confirmation", settings=settings)
    for _ in range(3):
        advance_conversations(db, settings)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        same = receive_message(wb, task, text="确认方案", key="late-confirmation", settings=settings)
        assert same["message_id"] == receipt["message_id"] and same["status"] == "completed"
        answer = wb.latest(task, "conversation_result").payload["text"]
        if lock:
            assert "补读工作区清单" in answer and "不能启动实施" in answer
            assert conversation_view(wb, task)["pending_action"]["kind"] == "execution_blocked"
        else:
            assert "核对方案并继续" in answer and "确认方案并开始" in answer
            assert conversation_view(wb, task)["pending_action"]["kind"] == "execute"
        assert [(e.id, e.record_hash) for e in wb.history(task)[:len(original)]] == original
        assert wb.latest_plan(task).record_hash == plan.record_hash
        assert not wb.jobs(task) and not wb.latest(task, "execution_authorized")
        assert not session.scalar(select(PlanApproval)) and not session.scalar(select(ExecutionAttempt))
        assert conversation_view(wb, task)["pending_count"] == 0
        assert len([e for e in wb.history(task) if e.kind == "conversation_finished"]) == 1
    db.close()


def test_confirmation_without_plan_does_not_invent_or_start_one(tmp_path):
    db, settings, task, *_ = seeded(tmp_path)
    request(db, settings, task, "确认方案")
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert "还没有可确认" in wb.latest(task, "conversation_result").payload["text"]
        assert not wb.jobs(task) and not wb.latest_plan(task)
    db.close()


def test_confirmation_api_remains_authenticated_and_replayable(tmp_path):
    db, settings, task, _ = saved_plan(tmp_path, lock="yarn.lock")
    settings = replace(settings, local_access_token="confirmation-local-token")
    path = f"/api/v1/tasks/{task}/workbench/conversation/messages"
    headers = {"X-ContribOS-Token": settings.local_access_token, "Idempotency-Key": "confirmation"}
    body = {"text": "确认方案"}
    with TestClient(create_app(settings)) as client:
        assert client.post(path, json=body).status_code == 401
        assert client.post(path, json=body, headers={**headers, "Origin": "https://foreign.invalid"}).status_code == 403
        first = client.post(path, json=body, headers=headers)
        assert first.status_code == 202
        advance_conversations(db, settings)
        replay = client.post(path, json=body, headers=headers)
        assert replay.status_code == 202 and replay.json()["message_id"] == first.json()["message_id"]
        view = client.get(f"/api/v1/tasks/{task}/workbench").json()
        assert view["conversation"]["pending_count"] == 0 and not view["busy"]
        assert view["execution"]["preflight"]["status"] == "blocked"
        assert "补读工作区清单" in view["conversation"]["results"][-1]["text"]
        assert not view["jobs"]
    db.close()


def test_source_bound_confirmation_is_a_question_answer(tmp_path):
    from tests.test_conversation_answers import question_fixture
    db, settings, task, _, binding = question_fixture(tmp_path)
    with db.session() as session:
        receive_message(PlanningService(session, settings.artifact_root), task,
                        text="确认方案", key="question-answer", settings=settings, **binding)
    advance_conversations(db, settings)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert [j.kind for j in wb.jobs(task)] == ["planning_context"]
        assert not wb.latest(task, "execution_authorized")
    db.close()


@pytest.mark.parametrize("change", ["busy", "stale", "paused"])
def test_confirmation_rechecks_current_plan_conditions(tmp_path, change):
    db, settings, task, _ = saved_plan(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        if change == "busy":
            wb.enqueue(task, "planning_turn", {}, key="active")
        elif change == "stale":
            context = wb.latest(task, "context_ready")
            wb.append(task, "context_ready", {**context.payload, "base_sha": "c" * 40}, key="new-context")
        else:
            wb.append(task, "conversation_paused", {"version": "conversation-v1"}, key="pause")
    request(db, settings, task, "确认方案")
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        answer = wb.latest(task, "conversation_result").payload["text"]
        assert {"busy": "仍在处理", "stale": "依据已变化", "paused": "已停止"}[change] in answer
        assert not wb.latest(task, "execution_authorized")
        assert len(wb.jobs(task)) == (1 if change == "busy" else 0)
    db.close()


def test_environment_request_reasoning_receives_current_blockers_and_no_execution_tool(tmp_path):
    db, settings, task, _ = saved_plan(tmp_path, lock="yarn.lock")
    request(db, settings, task, "那你准备node环境")
    reply = "项目使用 Yarn，需要先补读配置以冻结环境，当前没有启动实施。"
    job, runner = run(db, settings, {"reply": reply, "action": "respond"})
    assert job.state == "succeeded", job.error_message
    prompt = json.loads(runner.invocations[0].prompt)
    execution = prompt["observation"]["execution_status"]
    assert execution["preflight_status"] == "blocked" and not execution["can_execute"]
    assert execution["environment"]["status"] == "pending_read"
    assert "package.json" in execution["environment"]["required_paths"]
    assert execution["confirmation_via_chat"] is False
    assert "execute" not in prompt["observation"]["available_actions"]
    advance_conversations(db, settings)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest(task, "conversation_result").payload["text"] == reply
        assert all(j.kind == "conversation_intent" for j in wb.jobs(task))
        assert not wb.latest(task, "execution_authorized")
    db.close()


def test_environment_change_invalidates_reasoning_basis(tmp_path):
    db, settings, task, _ = saved_plan(tmp_path, lock="yarn.lock")
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        before = observation(wb, task)["basis_hash"]
        context = wb.latest(task, "context_ready")
        evidence = wb.read(context.payload["artifact_id"])
        evidence["inventory"].append("package-lock.json")
        wb.append(task, "context_ready", {**context.payload, "artifact_id": wb.artifact(evidence)}, key="new-environment")
        assert observation(wb, task)["basis_hash"] != before
    db.close()


@pytest.mark.parametrize("lock", ["yarn.lock", "pnpm-lock.yaml"])
def test_node_manager_discovers_original_lockfile_without_converting_it(lock):
    selected = environment_plan(["package.json", lock])
    assert selected["status"] == "supported" and selected["mode"] == "workspace-v2"
    assert lock in selected["manifests"] and "package-lock.json" not in selected["manifests"]


@pytest.mark.parametrize("lock", ["yarn.lock", "pnpm-lock.yaml"])
def test_workspace_package_uses_inherited_lockfile(lock):
    target = "excalidraw-app/data/tabSync.ts"
    selected = environment_plan(["package.json", lock, "excalidraw-app/package.json", target],
                                change_paths=[target])
    assert selected["status"] == "supported" and selected["project_directory"] == "."
    assert lock in selected["manifests"]
