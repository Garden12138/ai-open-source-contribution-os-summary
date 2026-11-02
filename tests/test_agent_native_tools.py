"""Offline protocol/binding tests; no live model or native delivery claim."""
import asyncio
import copy
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.agent_v2.contracts import TaskSpec
from app.agent_v2.native_tools import (
    BLOCKED, FINISH, INSTRUCTION, NativeToolsModelAdapter, native_input_bound,
    native_model_transport, native_tool_definitions, parse_native_decision,
)
from app.agent_v2.tools import RestrictedToolRegistry
from app.api import create_app
from app.database import Database
from app.migrations.versions import MIGRATIONS
from app.model_settings import ModelSettingsService, DEEPSEEK_MODELS
from app.planner import PlanningService
from app.provenance import canonical_json, content_hash
from app.providers.contracts import ProviderRunError
from app.providers.studio import StudioRunner, profile_identity
from app.schemas import PlanVersionCreateRequest
from app.security import SensitiveDataError
from tests.test_agent_v2_tools import tool_spec
from tests.test_model_settings import configure, CANARY
from tests.test_workbench import seeded


def response(name="read_file", arguments=None):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "reasoning_content": "PRIVATE_REASONING_SENTINEL",
        "tool_calls": [{"id": "call-one", "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments if arguments is not None else {"path": "calc.py"})}}]}}],
        "usage": {"prompt_tokens": 200, "completion_tokens": 80}}


def parse(value, request_id="request-one"):
    return parse_native_decision(value, spec=tool_spec(), request_id=request_id)


def test_registered_functions_are_exact_existing_capabilities_and_strict_terminals():
    definitions = native_tool_definitions()
    functions = {d["function"]["name"]: d["function"] for d in definitions}
    assert set(functions) == set(RestrictedToolRegistry.schemas) | {FINISH, BLOCKED}
    for name, schema in RestrictedToolRegistry().descriptions().items():
        assert functions[name]["parameters"] == schema
    assert functions["run_approved_checks"]["parameters"]["additionalProperties"] is False
    assert functions["run_approved_checks"]["parameters"]["properties"] == {}
    assert native_model_transport().tools_schema_hash == content_hash({"tools": definitions, "instruction": INSTRUCTION, "max_calls": 4})


def test_known_calls_are_typed_namespaced_and_never_expose_private_reasoning():
    raw = response()
    raw["choices"][0]["message"]["tool_calls"].append({"id": "call-two", "type": "function", "function": {"name": "list_paths", "arguments": "{}"}})
    decision, usage = parse(raw)
    assert [c.name for c in decision.calls] == ["read_file", "list_paths"]
    assert usage == {"input_tokens": 200, "output_tokens": 80}
    assert decision == parse(raw)[0]
    assert decision.calls[0].call_id != parse(raw, "other-request")[0].calls[0].call_id
    assert "PRIVATE_REASONING_SENTINEL" not in decision.model_dump_json()


@pytest.mark.parametrize("name,outcome", [(FINISH, "finish"), (BLOCKED, "blocked")])
def test_terminal_is_only_a_decision_request(name, outcome):
    decision, _ = parse(response(name, {"reply": "Evidence required"}))
    assert decision.outcome == outcome and decision.calls == []


@pytest.mark.parametrize("name,args", [
    ("shell", {"command": "true"}), ("contribos_record_structured_output", {"outcome": "finish"}),
    ("publish", {}), ("run_approved_checks", {"argv": ["sh"]}),
    ("apply_patch", {"path": "outside.py", "expected_hash": None, "old_text": "", "new_text": "x"}),
    ("read_file", {"path": "../private"}), ("read_file", {"path": ".git/hooks/pre-commit"}),
    ("read_file", {"path": "calc.py", "max_bytes": 99999999}),
    (FINISH, {"reply": "ok", "approved": True}),
])
def test_unknown_functions_and_widened_arguments_fail_closed(name, args):
    with pytest.raises(ProviderRunError) as caught:
        parse(response(name, args))
    assert not caught.value.retryable
    assert caught.value.usage.input_tokens == 200
    assert name not in str(caught.value.diagnostic) or name in {"read_file", FINISH}


@pytest.mark.parametrize("kind", ["duplicates", "five", "mixed", "bad_json", "duplicate_key", "nan", "list_args", "object_args", "role", "finish", "length", "usage", "choices", "id"])
def test_untrusted_envelopes_are_rejected_without_raw_arguments_or_reasoning(kind):
    raw = response()
    choice = raw["choices"][0]
    calls = choice["message"]["tool_calls"]
    if kind == "duplicates": calls.append(copy.deepcopy(calls[0]))
    elif kind == "five":
        choice["message"]["tool_calls"] = [{**copy.deepcopy(calls[0]), "id": str(i)} for i in range(5)]
    elif kind == "mixed":
        calls.append({"id": "terminal", "type": "function", "function": {"name": FINISH, "arguments": "{}"}})
    elif kind in {"bad_json", "duplicate_key", "nan", "list_args", "object_args"}:
        calls[0]["function"]["arguments"] = {"bad_json": "RAW_ARGUMENT_SENTINEL", "duplicate_key": '{"path":"calc.py","path":"other.py"}', "nan": '{"path":NaN}', "list_args": "[]", "object_args": {"path": "calc.py"}}[kind]
    elif kind == "role": choice["message"]["role"] = "tool"
    elif kind == "finish": choice["finish_reason"] = "stop"
    elif kind == "length": choice["finish_reason"] = "length"
    elif kind == "usage": raw["usage"]["prompt_tokens"] = True
    elif kind == "choices": raw["choices"].append(copy.deepcopy(choice))
    elif kind == "id": calls[0]["id"] = ""
    with pytest.raises(ProviderRunError) as caught:
        parse(raw)
    assert not caught.value.retryable
    assert "PRIVATE_REASONING_SENTINEL" not in str(caught.value.diagnostic)
    assert "RAW_ARGUMENT_SENTINEL" not in str(caught.value.diagnostic)


def test_secret_arguments_are_rejected_and_legacy_taskspec_wire_is_unchanged():
    with pytest.raises(SensitiveDataError):
        parse(response("search_code", {"query": CANARY}))
    old = tool_spec()
    assert "model_transport" not in old.model_dump()
    assert TaskSpec.model_validate_json(old.model_dump_json()) == old
    new = old.model_copy(update={"model_transport": native_model_transport()})
    assert new.model_dump()["model_transport"] == native_model_transport().model_dump()
    assert content_hash(old.model_dump()) != content_hash(new.model_dump())


def test_native_runner_posts_exact_functions_once_with_frozen_profile_and_no_wrapper(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'model.db'}")
    db.create_schema()
    with db.session() as session:
        row = configure(session, provider="deepseek", model=DEEPSEEK_MODELS[0])
        profile = ModelSettingsService(session).profile(row.id)
    requests, grants = [], []
    def handler(request):
        requests.append(json.loads(request.content))
        assert request.url == "http://gateway.test/v1/chat/completions"
        return httpx.Response(200, json=response())
    class Broker:
        def issue(self, invocation):
            grants.append(invocation)
            return SimpleNamespace(base_url="http://gateway.test/v1", token="short-lived-fixture")
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runner = StudioRunner(profile, broker=Broker(), client=client)
            spec = tool_spec().model_copy(update={"model_transport": native_model_transport()})
            adapter = NativeToolsModelAdapter(runner, profile_identity(profile), spec)
            prompt = {"goal": "Inspect source"}
            assert adapter.input_bound(prompt) == native_input_bound(prompt)
            assert native_input_bound(prompt) >= len(canonical_json(native_tool_definitions()).encode()) + len(canonical_json(prompt).encode())
            return await adapter.decide(prompt, request_id="fixture-request", max_output_tokens=4096)
    decision, usage = asyncio.run(run())
    assert decision.calls[0].name == "read_file" and usage["input_tokens"] == 200
    assert len(requests) == len(grants) == 1
    request = requests[0]
    assert request["tools"] == native_tool_definitions() and request["tool_choice"] == "auto"
    assert request["max_tokens"] == 4096 and request["stream"] is False
    assert request["_contribos_profile"] == profile
    assert request["messages"][0]["content"] == INSTRUCTION
    assert "REQUIRED_SCHEMA" not in canonical_json(request) and CANARY not in canonical_json(request)
    db.close()


def native_plan(tmp_path):
    db, settings, task, content, context = seeded(tmp_path)
    settings = replace(settings, agent_runtime_v2_enabled=True)
    with db.session() as session:
        configure(session, provider="deepseek", model=DEEPSEEK_MODELS[0])
        wb = PlanningService(session, settings.artifact_root)
        plan = wb.save(task, PlanVersionCreateRequest(**content.hash_payload()), context_hash=context.record_hash,
            key="native-plan", runtime_version="agent-runtime-v2")
        plan_id = plan.id
        session.commit()
    return db, settings, task, plan_id


def test_new_native_binding_preview_requires_exact_transport_hash_and_preserves_idempotency(tmp_path):
    db, settings, task, plan_id = native_plan(tmp_path)
    route = f"/api/v1/tasks/{task}/workbench"
    with TestClient(create_app(settings)) as client:
        view = client.get(route).json()
        transport = native_model_transport().model_dump()
        assert view["execution"]["model_transport"] == transport
        assert view["execution"]["preflight"]["agent_model_transport"] == transport
        payload = {"plan_id": plan_id, "plan_hash": view["plans"][-1]["record_hash"], "approve_plan": True,
            "start_execution": True, "model_binding_hash": view["model_binding_hash"]}
        for wrong in (None, "a" * 64):
            rejected = client.post(route + "/execute", json={**payload, "model_transport_hash": wrong}, headers={"Idempotency-Key": "native-exec"})
            assert rejected.status_code == 409, rejected.text
        with db.session() as session:
            assert not PlanningService(session, settings.artifact_root).latest(task, "execution_authorized")
        payload["model_transport_hash"] = view["execution"]["model_transport_hash"]
        approved = client.post(route + "/execute", json=payload, headers={"Idempotency-Key": "native-exec"})
        assert approved.status_code == 202, approved.text
        assert client.post(route + "/execute", json=payload, headers={"Idempotency-Key": "native-exec"}).json()["id"] == approved.json()["id"]
        payload["model_transport_hash"] = "b" * 64
        assert client.post(route + "/execute", json=payload, headers={"Idempotency-Key": "native-exec"}).status_code == 409
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        auth = wb.latest(task, "execution_authorized")
        assert auth.payload["agent_model_transport"] == transport
        assert len([e for e in wb.history(task) if e.kind == "execution_authorized"]) == 1
        changed = dict(auth.payload)
        changed.pop("agent_model_transport")
        with pytest.raises(IntegrityError):
            wb.append(task, "execution_authorized", changed, key="stripped-native-contract", plan_id=plan_id)
        session.rollback()


@pytest.mark.parametrize("change", ["runtime_disabled", "catalog_changed"])
def test_native_frozen_plan_rejects_runtime_or_catalog_changes(tmp_path, monkeypatch, change):
    db, settings, task, plan_id = native_plan(tmp_path)
    if change == "runtime_disabled": settings = replace(settings, agent_runtime_v2_enabled=False)
    else:
        from app.agent_v2 import native_tools
        monkeypatch.setattr(native_tools, "INSTRUCTION", INSTRUCTION + " changed")
    from app.contribution_workflow import authorize_execution
    from app.workbench import WorkbenchError
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        with pytest.raises(WorkbenchError) as caught:
            authorize_execution(wb, task, plan_id=plan_id, plan_hash=wb.latest_plan(task).record_hash, key="stale-native", settings=settings)
        assert caught.value.code == "agent_model_transport_stale"
        assert not wb.latest(task, "execution_authorized")


def test_native_worker_uses_existing_journal_budgets_and_finish_verification(tmp_path):
    from sqlalchemy import select
    from app.agent_v2.adapter import model_adapter, StructuredModelAdapter
    from app.agent_v2.journal import AgentJournal
    from app.agent_v2.loop import AgentModelWorker, AgentToolWorker, schedule
    from app.agent_v2.orchestrator import TaskOrchestrator
    from app.contribution_workflow import advance_workflows
    from app.models import ExecutionAttempt
    from tests.test_workbench import automatic_fixture
    from tests.test_agent_v2_loop import FixtureGateway
    def models(db, settings, task):
        with db.session() as session: configure(session, provider="deepseek", model=DEEPSEEK_MODELS[0])
    db, settings, task, workers, _, _ = automatic_fixture(tmp_path, configure_models=models, runtime_v2=True, native_transport=True)
    for worker in (workers[0], workers[4]):
        job = asyncio.run(worker.run_once())
        assert job.state == "succeeded", job.error_message
        advance_workflows(db, settings)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        attempt = session.scalar(select(ExecutionAttempt).where(ExecutionAttempt.task_id == task))
        j = TaskOrchestrator(wb, settings).start(attempt.id)
        assert j.spec().model_transport == native_model_transport()
        profile = ModelSettingsService(session).profile(ModelSettingsService(session).task_profiles(task)["implementation"])
        identity = profile_identity(profile)
        assert isinstance(model_adapter(object(), identity, tool_spec()), StructuredModelAdapter)
        schedule(j)
        sid = j.session_id
        session.commit()
    class Runner:
        responses = [response("read_file", {"path": "app/plans.py"}), response(FINISH, {"reply": "All checks passed"})]
        async def complete_native_tools(self, invocation, tools, instruction):
            return self.responses.pop(0), 1
    runner, gateway = Runner(), FixtureGateway()
    model = AgentModelWorker(db, settings, lambda _, spec: model_adapter(runner, identity, spec), worker_id="fixture-native-model")
    tool = AgentToolWorker(db, settings, gateway, worker_id="fixture-native-tool")
    for worker in (model, tool, model):
        job = asyncio.run(worker.run_once())
        assert job.state == "succeeded", job.error_message
    assert len(gateway.calls) == 1 and gateway.calls[0].name == "read_file"
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        j = AgentJournal(wb, task, sid)
        assert j.checkpoint().state == "ready" # finish cannot bypass actual edit/check evidence
        assert j.checkpoint().usage["model_calls"] == 2 and j.checkpoint().usage["reads"] == 1
        assert j.spec().limits.model_calls == 24
        assert wb.latest(task, "agent_v2_model_rejected") and not wb.latest(task, "automatic_change_accepted")


def test_upgrade_from_0040_preserves_events_reviews_and_restores_backup(tmp_path, monkeypatch):
    from app.migrations import versions
    from tests.test_workbench import automatic_fixture, populate_round, drive_round
    with monkeypatch.context() as patch:
        patch.setattr(versions, "MIGRATIONS", tuple(m for m in MIGRATIONS if m.revision <= "0040_deepseek_reviews"))
        db, settings, task, workers, code, review = automatic_fixture(tmp_path)
        populate_round(code, review)
        drive_round(db, settings, task, workers)
    backup = tmp_path / "before-0041.db"
    tables = ("review_runs", "workbench_events", "plan_versions", "plan_approvals", "execution_attempts")
    with db.engine.connect() as conn:
        raw = conn.connection.driver_connection
        old = {t: list(raw.execute("SELECT * FROM " + t)) for t in tables}
        assert all(old.values())
        with sqlite3.connect(backup) as target: raw.backup(target)
    assert db.create_schema().applied == ("0041_native_model_tools",)
    with db.engine.connect() as conn:
        for table in tables: assert list(conn.connection.driver_connection.execute("SELECT * FROM " + table)) == old[table]
        assert conn.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        for index, mode in enumerate((None, {}, {**native_model_transport().model_dump(), "extra": True},
                {"version": "agent-native-tools-v1", "tools_schema_hash": "z" * 64})):
            with pytest.raises(IntegrityError):
                wb.append(task, "plan_bound", {"agent_model_transport": mode}, key="malformed-native-" + str(index))
            session.rollback()
    db.close()
    restored_db = Database("sqlite:///" + str(backup))
    assert restored_db.current_revision() == "0040_deepseek_reviews"
    assert restored_db.create_schema().applied == ("0041_native_model_tools",)
    with restored_db.engine.connect() as conn:
        for table in tables: assert list(conn.connection.driver_connection.execute("SELECT * FROM " + table)) == old[table]
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    restored_db.close()
