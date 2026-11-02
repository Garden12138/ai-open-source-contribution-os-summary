"""Current-plan projections and opt-in compatibility at the existing API seam."""
from dataclasses import replace
import hashlib

from app.config import environment_engine_configuration
from app.execution_preflight import task_preflight
from app.planner import PlanningService
from app.schemas import PlanVersionCreateRequest
from app.workbench_view import workbench_view
from tests.test_execution_preflight import project_context
from tests.test_workbench import seeded


def test_new_contract_is_static_only_and_disable_never_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("UPEVE_ENABLED", "1")
    monkeypatch.setenv("UPEVE_TARGET_PLATFORM", "linux/arm64")
    db, settings, task, content, context = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        context = project_context(wb, task, context,
            '[project]\nname="foreign-layout"\nversion="1"\n[dependency-groups]\ntest=["pytest"]\n')
        data = content.hash_payload()
        data.pop("schema_version")
        plan = wb.persist_plan(task, PlanVersionCreateRequest(**data), context, key="upeve-plan", actor="local-user",
            environment_engine=environment_engine_configuration())
        session.commit()
        report = task_preflight(wb, task)
        assert report["status"] == "ready", report["issues"]
        assert report["readiness_label"] == "static_admitted"
        view = workbench_view(wb, task)["environment_engine"]
        assert view["stages"]["preparation"] == view["stages"]["baseline"] == "unverified"
        assert not view["executables"]
        old = [(e.id, e.record_hash) for e in wb.history(task)]
        monkeypatch.setenv("UPEVE_ENABLED", "0")
        disabled = task_preflight(wb, task)
        assert disabled["status"] == "blocked"
        assert disabled["issues"][0]["code"] == "environment_engine_disabled"
        assert [(e.id, e.record_hash) for e in wb.history(task)] == old
        assert wb.latest_plan(task).record_hash == plan.record_hash


def test_old_binding_and_preflight_hash_stay_exact_when_switch_enabled(tmp_path, monkeypatch):
    from tests.test_workbench import result, run_turn
    db, settings, task, content, _ = seeded(tmp_path)
    with db.session() as session:
        PlanningService(session, settings.artifact_root).request(task, text="plan", parent_id=None, expected_hash=None, key="legacy")
    run_turn(db, settings, result(content))
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        before = task_preflight(wb, task)
        monkeypatch.setenv("UPEVE_ENABLED", "1")
        monkeypatch.setenv("UPEVE_TARGET_PLATFORM", "linux/amd64")
        assert task_preflight(wb, task) == before
        assert workbench_view(wb, task)["environment_engine"] is None


def test_configuration_requires_explicit_platform(monkeypatch):
    import pytest
    monkeypatch.setenv("UPEVE_ENABLED", "1")
    monkeypatch.delenv("UPEVE_TARGET_PLATFORM", raising=False)
    with pytest.raises(ValueError):
        environment_engine_configuration()


def test_CI_toolchain_declarations_remain_static_evidence_in_API_projection():
    from types import SimpleNamespace
    from app.environment_engine.view import environment_view
    declarations = [{"tool": "pnpm", "range": "10", "path": ".github/workflows/check.yml", "line": 8}]
    wb = SimpleNamespace(latest=lambda *_: None)
    selected = {"environment_engine": {"version": "upeve-v1"}, "verification_contract": {
        "admission": "static_admitted", "preparation": {"toolchain_declarations": declarations}}}
    view = environment_view(wb, "fixture", selected, None, prepared=False)
    assert view["toolchain_declarations"] == declarations
    assert view["stages"]["preparation"] == view["stages"]["baseline"] == "unverified"
