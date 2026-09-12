from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from app.planner import PlanningReply, PlanningService
from app.planning_diagnostics import planning_validation_details
from app.providers.nvidia_nim import _nvidia_tool_schema
from tests.test_workbench import seeded, result, run_turn

CANARY = "ghp_diagnosticcanary123456789012345678"


@pytest.mark.parametrize(
    "case,expected",
    [
        ("missing", "planning_outcome_missing"),
        ("conflict", "planning_outcome_conflict"),
        ("duplicates", "planning_question_ids_duplicate"),
        ("sensitive", "planning_sensitive_output"),
        ("extra", "planning_output_invalid"),
        ("json", "planning_output_invalid"),
        ("reply_object", "planning_output_invalid"),
        ("step_object", "planning_output_invalid"),
        ("test_object", "planning_output_invalid"),
    ],
)
def test_invalid_replies_emit_only_safe_diagnostics(tmp_path, caplog, case, expected):
    database, settings, task_id, content, _ = seeded(tmp_path)
    value = json.loads(result(content))
    if case == "missing":
        value["plan"] = None
    elif case == "conflict":
        value["read_paths"] = ["app/planning.py"]
    elif case == "duplicates":
        value["plan"] = None
        value["questions"] = [{"id": "choice", "prompt": "请选择", "options": []}] * 2
    elif case == "sensitive":
        value["reply"] = CANARY
    elif case == "extra":
        value[CANARY] = CANARY
    elif case == "reply_object":
        value["reply"] = {"description": CANARY}
    elif case == "step_object":
        value["plan"]["implementation_steps"] = [{"description": CANARY}]
    elif case == "test_object":
        value["plan"]["tests_to_add_or_run"] = [{"description": CANARY}]
    reply = CANARY if case == "json" else json.dumps(value)
    with database.session() as session:
        PlanningService(session, settings.artifact_root).request(
            task_id,
            text="修改方案",
            parent_id=None,
            expected_hash=None,
            key="diagnostic",
        )
    failed, runner = run_turn(database, settings, reply)
    assert failed.state == "failed" and failed.error_code == expected
    assert len(runner.invocations) == 1  # No hidden repair call or retry.
    with database.session() as session:
        workbench = PlanningService(session, settings.artifact_root)
        diagnostic = workbench.latest(task_id, "model_output_rejected")
        assert diagnostic.payload["reason_code"] == expected
        assert diagnostic.job_id == failed.id
        expected_fields = {
            "reply_object": ["reply"],
            "step_object": ["plan.implementation_steps"],
            "test_object": ["plan.tests_to_add_or_run"],
        }
        if case in expected_fields:
            assert diagnostic.payload["fields"] == expected_fields[case]
        assert workbench.latest_plan(task_id) is None
        assert workbench.latest(task_id, "assistant_message") is None
        with database.engine.connect() as connection:
            stored = "\n".join(connection.connection.driver_connection.iterdump())
        assert CANARY not in stored
    assert CANARY not in caplog.text and CANARY not in failed.error_message
    database.close()


def test_custom_validation_type_and_field_are_not_copied():
    error = ValidationError.from_exception_data(
        "untrusted",
        [
            {
                "type": PydanticCustomError(CANARY, CANARY),
                "loc": ("plan", CANARY),
                "input": CANARY,
            }
        ],
    )
    details = planning_validation_details(error)
    assert details == {
        "reason_code": "planning_output_invalid",
        "validation_types": ["unknown_validation_error"],
        "fields": ["response"],
    }


def test_provider_schema_expresses_the_existing_exclusive_outcomes():
    schema = PlanningReply.model_json_schema()
    projected = _nvidia_tool_schema(schema)
    assert projected["oneOf"] == schema["oneOf"]
    assert [branch["required"] for branch in schema["oneOf"]] == [
        ["questions"],
        ["read_paths"],
        ["plan"],
    ]
    assert schema["oneOf"][0]["properties"]["plan"] == {"type": "null"}
    assert schema["oneOf"][1]["properties"]["questions"] == {"maxItems": 0}
    assert schema["oneOf"][2]["properties"]["questions"] == {"maxItems": 0}
    assert schema["oneOf"][2]["properties"]["read_paths"] == {"maxItems": 0}
    assert "reply" not in schema.get("required", [])
    assert "Optional top-level" in schema["properties"]["reply"]["description"]
    plan = schema["$defs"]["PlanningPlan"]["properties"]
    for field in ("implementation_steps", "tests_to_add_or_run"):
        assert plan[field]["items"] == {"type": "string"}
        assert "plain strings" in plan[field]["description"]


@pytest.mark.parametrize("omit", [True, False])
def test_valid_plan_without_narration_is_saved_without_execution(tmp_path, omit):
    database, settings, task_id, content, _ = seeded(tmp_path)
    value = json.loads(result(content))
    value.pop("reply")
    if not omit:
        value["reply"] = ""
    with database.session() as session:
        PlanningService(session, settings.artifact_root).request(
            task_id, text="修改方案", parent_id=None, expected_hash=None, key="no-narration"
        )
    completed, runner = run_turn(database, settings, json.dumps(value))
    assert completed.state == "succeeded"
    assert len(runner.invocations) == 1
    with database.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        plan = wb.latest_plan(task_id)
        assert plan.goal == content.goal
        assert wb.latest(task_id, "assistant_message").payload["reply"] == ""
        assert not wb.latest(task_id, "execution_authorized")
        assert not wb.latest(task_id, "model_output_rejected")
    database.close()


@pytest.mark.parametrize("value,code", [
    ({}, "planning_outcome_missing"),
    ({"read_paths": ["app.py"], "questions": [{"id": "choose", "prompt": "选择"}]}, "planning_outcome_conflict"),
    ({"plan": {"goal": "incomplete"}}, "planning_output_invalid"),
    ({"questions": [{"id": "choose", "prompt": CANARY}]}, "planning_sensitive_output"),
])
def test_omitted_narration_does_not_bypass_outcome_checks(value, code):
    with pytest.raises(ValidationError) as caught:
        PlanningReply.model_validate(value)
    assert planning_validation_details(caught.value)["reason_code"] == code


@pytest.mark.parametrize("value", [
    {"read_paths": ["app.py"]},
    {"questions": [{"id": "choose", "prompt": "选择"}]},
])
def test_other_valid_outcomes_without_narration(value):
    assert PlanningReply.model_validate(value).reply == ""
