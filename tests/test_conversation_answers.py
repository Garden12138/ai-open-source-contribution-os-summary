"""Source-bound clarification answers resume planning, never grant approval."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.conversation import advance_conversations, conversation_view, receive_message
from app.planner import PlanningService
from app.workbench import WorkbenchError
from tests.test_workbench import seeded


def question_fixture(tmp_path):
    db, settings, task, _, context = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        evidence = wb.read(context.payload["artifact_id"])
        evidence["inventory"].append("src/unread.py")
        wb.append(task, "context_ready", {**context.payload, "artifact_id": wb.artifact(evidence)}, key="unread")
        source = wb.append(task, "assistant_message", {
            "reply": "请确认范围", "questions": [{"id":"scope", "prompt":"修改范围？", "options":["只改当前包"]}],
            "read_paths":["src/unread.py"], "plan_id":None,
        }, key="scope")
        binding = {"question_source_id":source.id, "question_source_hash":source.record_hash}
    return db, settings, task, source, binding


def test_bound_answer_api_runs_deferred_read_without_classifier_and_is_idempotent(tmp_path):
    db, settings, task, source, binding = question_fixture(tmp_path)
    settings = replace(settings, local_access_token="test-local-token")
    url = f"/api/v1/tasks/{task}/workbench/conversation/messages"
    headers = {"X-ContribOS-Token":settings.local_access_token, "Idempotency-Key":"answer"}
    body = {"text":"只改当前包", **binding}
    with TestClient(create_app(settings)) as client:
        assert client.post(url, json=body).status_code == 401
        assert client.post(url, json=body, headers={**headers,"Origin":"https://foreign.invalid"}).status_code == 403
        received = client.post(url, json=body, headers=headers)
        assert received.status_code == 202, received.text
        db.create_schema()  # Additive event fields survive upgrade with the same receipt.
        assert client.post(url, json=body, headers=headers).json() == received.json()
        advance_conversations(db, settings)
        repeated = client.post(url, json=body, headers=headers)
        assert repeated.status_code == 202 and repeated.json()["message_id"] == received.json()["message_id"]
        assert repeated.json()["status"] == "processing"
        assert client.post(url, json={"text":body["text"]}, headers=headers).status_code == 409
        assert client.post(url, json=body, headers={**headers,"Idempotency-Key":"different-key"}).status_code == 409
    advance_conversations(db, settings)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        jobs = wb.jobs(task)
        assert len(jobs) == 1 and jobs[0].kind == "planning_context"
        assert jobs[0].payload["paths"] == ["src/unread.py"]
        assert jobs[0].payload["text"] == body["text"]
        assert len([e for e in wb.history(task) if e.kind == "conversation_received"]) == 1
        assert wb.latest(task,"conversation_received").payload["question_source"] == {"id":source.id,"record_hash":source.record_hash}
        assert not wb.latest(task,"model_call") and not wb.latest(task,"execution_authorized")
        assert conversation_view(wb,task)["pending_action"] is None


@pytest.mark.parametrize("change", ["hash", "source", "new_question", "paused", "answered", "busy"])
def test_answer_rejects_stale_or_unavailable_question_without_jobs(tmp_path, change):
    db, settings, task, source, binding = question_fixture(tmp_path)
    with db.session() as session:
        wb = PlanningService(session,settings.artifact_root)
        if change == "hash": binding["question_source_hash"] = "0"*64
        elif change == "source": binding["question_source_id"] = "another-task-question"
        elif change == "new_question": wb.append(task,"assistant_message",source.payload,key="new-question")
        elif change == "paused": wb.append(task,"conversation_paused",{"version":"conversation-v1"},key="paused")
        elif change == "answered": receive_message(wb,task,text="已经回答",key="earlier",settings=settings)
        elif change == "busy": wb.enqueue(task,"planning_turn",{},key="active")
        before = [(e.id,e.record_hash) for e in wb.history(task)]
        jobs = [j.id for j in wb.jobs(task)]
        with pytest.raises(WorkbenchError) as error:
            receive_message(wb,task,text="只改当前包",key="answer",settings=settings,**binding)
        assert error.value.code == "conversation_question_stale"
        assert [(e.id,e.record_hash) for e in wb.history(task)] == before
        assert [j.id for j in wb.jobs(task)] == jobs


def test_answer_binding_is_rechecked_before_dispatch(tmp_path):
    db,settings,task,source,binding=question_fixture(tmp_path)
    with db.session() as session:
        wb=PlanningService(session,settings.artifact_root)
        receive_message(wb,task,text="只改当前包",key="answer",settings=settings,**binding)
        wb.append(task,"assistant_message",source.payload,key="superseded")
    advance_conversations(db,settings)
    with db.session() as session:
        wb=PlanningService(session,settings.artifact_root)
        assert not wb.jobs(task)
        view=conversation_view(wb,task)
        assert view["messages"][0]["receipt"]["status"] == "failed"
        assert view["pending_count"] == 0


def test_custom_answer_text_cannot_invoke_stop_or_publication(tmp_path):
    db,settings,task,_,binding=question_fixture(tmp_path)
    with db.session() as session:
        wb=PlanningService(session,settings.artifact_root)
        receive_message(wb,task,text="停止",key="answer",settings=settings,**binding)
        assert not wb.latest(task,"conversation_paused")
    advance_conversations(db,settings)
    with db.session() as session:
        wb=PlanningService(session,settings.artifact_root)
        assert [j.kind for j in wb.jobs(task)] == ["planning_context"]
        assert not wb.latest(task,"stop_requested") and not wb.latest(task,"publication_confirmed")


@pytest.mark.parametrize("partial",[{"question_source_id":"source"},{"question_source_hash":"a"*64}])
def test_question_binding_requires_both_fields(tmp_path,partial):
    db,settings,task,_,_=question_fixture(tmp_path)
    settings=replace(settings,local_access_token="test-local-token")
    with TestClient(create_app(settings)) as client:
        response=client.post(f"/api/v1/tasks/{task}/workbench/conversation/messages",
            json={"text":"回答",**partial},headers={"X-ContribOS-Token":settings.local_access_token,"Idempotency-Key":"partial"})
        assert response.status_code == 422
