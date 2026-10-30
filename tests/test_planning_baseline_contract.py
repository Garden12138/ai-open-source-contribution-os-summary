"""Provider/API schema and strict Baseline semantics must describe the same wire."""
from dataclasses import replace
import json

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from app.api import create_app
from app.planner import PlanningPlan, PlanningReply, PlanningService, plan_content
from app.planning_diagnostics import planning_domain_details, planning_validation_details
from app.plans import PlanCommand
from app.provenance import canonical_json
from app.schemas import PlanCommandRequest
from tests.test_workbench import seeded, result, run_turn


def test_provider_schema_describes_exact_baseline_expectation():
    schema = PlanningReply.model_json_schema()
    field = schema["$defs"]["PlanCommandRequest"]["properties"]["baseline_expectation"]
    assert field["anyOf"] == [{"$ref": "#/$defs/BaselineExpectation"}, {"type": "null"}]
    expectation = schema["$defs"]["BaselineExpectation"]
    assert expectation["additionalProperties"] is False
    assert set(expectation["required"]) == {"issue_url", "test_node", "failure_literal"}
    assert expectation["properties"]["exit_code"]["const"] == 1


def test_valid_expectation_and_positive_commands_retain_legacy_domain_hashes():
    target = "tests.test_bug.Test.test_bug"
    expected = {"issue_url": "https://github.com/example/another/issues/42",
        "test_node": target, "failure_literal": "AssertionError: actual regression"}
    values = {"command_id": "reproduce", "purpose": "Reproduce existing defect",
        "argv": ["python", "-m", "unittest", target], "working_directory": "."}
    request = PlanCommandRequest(**values, baseline_expectation=expected)
    domain = PlanCommand(**{**request.model_dump(), "argv": tuple(request.argv)})
    old = PlanCommand(**{**values, "argv": tuple(values["argv"])}, baseline_expectation=expected)
    assert domain.hash_payload() == old.hash_payload()
    assert PlanCommandRequest.model_validate_json(request.model_dump_json()) == request
    positive = PlanCommandRequest(**values)
    assert positive.baseline_expectation is None
    assert "baseline_expectation" not in PlanCommand(**{
        **positive.model_dump(), "argv": tuple(positive.argv)}).hash_payload()


def test_actual_rejected_shape_has_safe_and_specific_diagnostics(tmp_path):
    db, _, _, content, _ = seeded(tmp_path)
    value = json.loads(result(content))["plan"]
    # Same shape retained in the real failed planning job: positive checks
    # were assigned an arbitrary expected/notes object absent from the schema.
    value["commands_to_run"][0]["baseline_expectation"] = {"expected": "passed", "notes": "Baseline must be green"}
    with pytest.raises(ValidationError) as caught:
        PlanningPlan.model_validate(value)
    details = planning_validation_details(caught.value)
    assert details["validation_types"] == ["extra_forbidden", "missing"]
    assert "commands_to_run.baseline_expectation" in details["fields"]
    assert "commands_to_run.baseline_expectation.issue_url" in details["fields"]
    assert "commands_to_run.baseline_expectation.test_node" in details["fields"]
    assert "response" not in details["fields"]
    assert "notes" not in canonical_json(details) and "Baseline must be green" not in canonical_json(details)
    db.close()


def test_invalid_expectation_repair_is_bounded_and_preserves_usage_without_execution(tmp_path):
    db, settings, task, content, _ = seeded(tmp_path)
    with db.session() as session:
        PlanningService(session, settings.artifact_root).request(task, text="Prepare a safe plan",
            parent_id=None, expected_hash=None, key="baseline-repair")
    value = json.loads(result(content))
    value["plan"]["commands_to_run"][0]["baseline_expectation"] = {"expected": "passed"}
    first, _ = run_turn(db, settings, json.dumps(value))
    assert first.state == "succeeded"
    assert first.result_data["plan_status"] == "rejected" and first.result_data["repair_scheduled"]
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest_plan(task) is None and not wb.latest(task, "execution_authorized")
        rejected = wb.latest(task, "model_output_rejected")
        assert "commands_to_run.baseline_expectation.issue_url" in rejected.payload["fields"]
    second, runner = run_turn(db, settings, result(content))
    assert second.state == "succeeded", second.error_message
    prompt = json.loads(runner.invocations[0].prompt)
    assert "commands_to_run.baseline_expectation.issue_url" in prompt["plan_repair"]["fields"]
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest_plan(task).content_hash == content.content_hash
        assert len([e for e in wb.history(task) if e.kind == "model_call"]) == 2
        assert not wb.latest(task, "execution_authorized")
    db.close()


def test_exact_test_node_requirement_is_not_weakened():
    with pytest.raises(ValueError) as caught:
        PlanCommand("reproduce", "Reproduce existing defect", ("python", "-m", "pytest"),
            baseline_expectation={"issue_url": "https://github.com/example/another/issues/42",
                "test_node": "tests/test_bug.py::test_bug", "failure_literal": "AssertionError: actual regression"})
    details = planning_domain_details(caught.value)
    assert details["constraint"] == "expected_reproduction_exact_target"
    assert details["fields"] == ["commands_to_run.baseline_expectation.test_node"]


def test_manual_plan_api_rejects_invalid_expectation_and_redacts_canary(tmp_path):
    db, settings, task, content, context = seeded(tmp_path)
    settings = replace(settings, local_access_token="test-local-token")
    route = f"/api/v1/tasks/{task}/workbench/plans"
    payload = {"context_hash": context.record_hash, "plan": json.loads(result(content))["plan"]}
    canary = "ghp_baselinecontractcanary123456789012345678"
    with TestClient(create_app(settings)) as client:
        headers = {"X-ContribOS-Token": "test-local-token", "Idempotency-Key": "baseline-schema"}
        payload["plan"]["commands_to_run"][0]["baseline_expectation"] = {canary: canary}
        invalid = client.post(route, json=payload, headers=headers)
        assert invalid.status_code == 422
        assert canary not in invalid.text
        payload["plan"]["commands_to_run"][0].pop("baseline_expectation")
        valid = client.post(route, json=payload, headers=headers)
        assert valid.status_code == 201, valid.text
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest_plan(task).content_hash == content.content_hash
        assert not wb.latest(task, "execution_authorized")
        dump = "\n".join(session.connection().connection.driver_connection.iterdump())
        assert canary not in dump
    db.close()


@pytest.mark.parametrize("recover", [True, False])
def test_v2_empty_model_outcome_uses_existing_bounded_repairs_without_inventing_content(tmp_path, recover):
    db, settings, task, content, _ = seeded(tmp_path)
    with db.session() as session:
        PlanningService(session, settings.artifact_root).request(task, text="Prepare a safe plan",
            parent_id=None, expected_hash=None, key="empty-repair", runtime_version="agent-runtime-v2")
    first, _ = run_turn(db, settings, '{}')
    assert first.state == 'succeeded' and first.result_data['repair_scheduled']
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest_plan(task) is None and not wb.latest(task, 'execution_authorized')
        assert wb.latest(task, 'model_output_rejected').payload['reason_code'] == 'planning_outcome_missing'
    second, runner = run_turn(db, settings, result(content) if recover else '{}')
    assert second.state == 'succeeded'
    assert json.loads(runner.invocations[0].prompt)['plan_repair']['reason_code'] == 'planning_outcome_missing'
    if not recover:
        final, _ = run_turn(db, settings, '{}')
        assert final.state == 'failed' and final.error_code == 'planning_outcome_missing'
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert len([e for e in wb.history(task) if e.kind == 'model_call']) == (2 if recover else 3)
        rejections = [e for e in wb.history(task) if e.kind == 'model_output_rejected']
        assert len(rejections) == (1 if recover else 3)
        assert all(e.payload.get('usage', {}).get('input_tokens', 0) > 0 for e in rejections)
        assert bool(wb.latest_plan(task)) is recover
        assert not wb.latest(task, 'execution_authorized')
        assert not any(j.state in ('queued', 'leased', 'running') for j in wb.jobs(task))
    db.close()
