from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import create_app
from app.model_settings import ModelSettingsService
from app.models import ModelConfigVersion
from app.planner import PlanningService
from tests.test_model_settings import configure
from tests.test_workbench import seeded, run_turn, result


@pytest.fixture
def task(tmp_path):
    db, settings, task_id, content, context = seeded(tmp_path)
    with db.session() as session:
        configure(session)
    route = f"/api/v1/tasks/{task_id}/workbench"
    with TestClient(create_app(settings)) as client:
        assert client.post(route + "/messages", json={"text": "方案"},
            headers={"Idempotency-Key": "start"}).status_code == 202
        assert run_turn(db, settings, result(content))[0].state == "succeeded"
        yield db, settings, task_id, content, context, client, route
    db.close()


def apply(client, route, detail, *, key="apply", profiles=None, expected=None):
    return client.post(route + "/models", json={
        "profiles": profiles if profiles is not None else detail["model_profiles"],
        "expected_hash": expected or detail["model_binding_hash"],
    }, headers={"Idempotency-Key": key})


def authorize(client, route, detail):
    plan = detail["plans"][-1]
    return client.post(route + "/execute", json={
        "plan_id": plan["id"], "plan_hash": plan["record_hash"],
        "approve_plan": True, "start_execution": True,
        "model_binding_hash": detail["model_binding_hash"],
    }, headers={"Idempotency-Key": "execute"})


def legacy_selection(db, settings, task_id, profiles=None, *, key="legacy"):
    with db.session() as session:
        service = ModelSettingsService(session)
        old = service.latest("task:" + task_id)
        row = service.append("task:" + task_id, profiles or old.payload,
            old.record_hash, commit=False)
        PlanningService(session, settings.artifact_root).append(task_id, "model_changed",
            {"profiles": row.payload, "binding_hash": row.record_hash}, key=key)


def test_noop_is_idempotent_and_keeps_plan_executable(task):
    db, settings, task_id, _, _, client, route = task
    before = client.get(route).json()
    for key in ("apply", "apply", "same-again"):
        response = apply(client, route, before, key=key)
        assert response.status_code == 200
        assert response.json() == {"record_hash": before["model_binding_hash"], "changed": False}
    with db.session() as session:
        versions = list(session.scalars(select(ModelConfigVersion).where(
            ModelConfigVersion.scope == "task:" + task_id)))
        assert len(versions) == 1
        wb = PlanningService(session, settings.artifact_root)
        assert not wb.latest(task_id, "model_changed")
        assert len([e for e in wb.history(task_id) if e.kind == "model_selection_applied"]) == 2
    assert client.get(route).json()["plans"] == before["plans"]
    # Only enqueue the fake fixture's authorized Job; no worker or external action.
    assert authorize(client, route, client.get(route).json()).status_code == 202


def test_stale_noop_is_rejected(task):
    db, settings, task_id, _, _, client, route = task
    before = client.get(route).json()
    assert apply(client, route, before, expected="a" * 64).status_code == 409
    with db.session() as session:
        assert not PlanningService(session, settings.artifact_root).latest(task_id, "model_selection_applied")


def test_noop_replay_after_real_change_does_not_restore_old_selection(task):
    db, _, _, _, _, client, route = task
    before = client.get(route).json()
    first = apply(client, route, before)
    with db.session() as session:
        second = configure(session, model="second")
    profiles = {**before["model_profiles"], "implementation": second.id}
    switched = apply(client, route, before, profiles=profiles, key="real")
    assert switched.json()["changed"] is True
    assert apply(client, route, before).json() == first.json()
    assert apply(client, route, before, profiles=profiles).status_code == 409
    assert client.get(route).json()["model_binding_hash"] == switched.json()["record_hash"]


def test_proven_historical_duplicate_no_longer_blocks_without_rewriting(task):
    db, settings, task_id, _, _, client, route = task
    legacy_selection(db, settings, task_id)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        records = [(e.id, e.record_hash, e.payload.copy()) for e in wb.history(task_id)]
        binding = wb.binding(task_id, wb.latest_plan(task_id).id)
        assert not wb.models_changed_after(task_id, binding.sequence)
    assert authorize(client, route, client.get(route).json()).status_code == 202
    with db.session() as session:
        history = PlanningService(session, settings.artifact_root).history(task_id)
        assert [(e.id, e.record_hash, e.payload) for e in history[:len(records)]] == records


@pytest.mark.parametrize("case", ["real_then_duplicate", "round_trip", "narrow", "unproven"])
def test_only_proven_noops_are_ignored(task, case):
    db, settings, task_id, _, _, client, route = task
    original = client.get(route).json()["model_profiles"]
    with db.session() as session:
        second = configure(session, model="second")
    if case in ("real_then_duplicate", "round_trip"):
        legacy_selection(db, settings, task_id, {**original, "review": second.id}, key="real")
        if case == "round_trip":
            legacy_selection(db, settings, task_id, original, key="return")
    else:
        with db.session() as session:
            PlanningService(session, settings.artifact_root).append(task_id, "model_changed",
                {"planning_profile_id": second.id} if case == "narrow" else
                {"profiles": original, "binding_hash": "a" * 64}, key="unknown")
    legacy_selection(db, settings, task_id)
    response = authorize(client, route, client.get(route).json())
    assert response.status_code == 409 and "模型已切换" in response.text


def test_real_switch_resaving_same_content_creates_child_plan(task):
    db, _, _, content, context, client, route = task
    before = client.get(route).json()
    with db.session() as session:
        second = configure(session, model="second")
    assert apply(client, route, before, profiles={**before["model_profiles"],
        "implementation": second.id}).json()["changed"]
    assert authorize(client, route, client.get(route).json()).status_code == 409
    payload = content.hash_payload()
    payload.pop("schema_version")
    payload["parent_version_id"] = before["plans"][-1]["id"]
    saved = client.post(route + "/plans", json={"context_hash": context.record_hash, "plan": payload},
        headers={"Idempotency-Key": "resave"})
    assert saved.status_code == 201, saved.text
    after = client.get(route).json()
    assert after["plans"][-1]["id"] != before["plans"][-1]["id"]
    assert after["plans"][-1]["parent_version_id"] == before["plans"][-1]["id"]
    assert authorize(client, route, after).status_code == 202
