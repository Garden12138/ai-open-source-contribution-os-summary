"""Verdicts require exact receipts; fakes here test contracts, never native E2E."""
from copy import deepcopy
from dataclasses import replace
import json
import sqlite3

import pytest
from sqlalchemy import select
from app.models import PlanVersion

from app.environment_engine.baseline import classify_baseline
from app.environment_engine.contracts import BaselineExpectation, VerificationEnvironmentContract
from app.plans import PlanCommand, PlanVersionService
from app.provenance import content_hash
from tests.test_environment_engine_resolution import contract


COMMANDS = [{"command_id": "tests", "argv": ["python", "-m", "unittest", "-v"], "working_directory": "."}]


def baseline_contract(commands=COMMANDS):
    return contract({"pyproject.toml": '[project]\nname="unrelated"\nversion="1"\n'}, commands).model_dump(mode="json")


def receipt(command=COMMANDS[0], *, code=0, output="Ran 3 tests in 0.1s\nOK", **extra):
    return {"command_id": command["command_id"], "command_hash": content_hash(command),
        "status": "passed" if code == 0 else "failed", "exit_code": code, "started_at": "2026-10-28T00:00:00Z",
        "stdout_log": output, "stderr_log": "", "logs_truncated": False, "raw_logs_omitted": False,
        "stdout_hash": content_hash(output), "stderr_hash": content_hash(""), **extra}


@pytest.mark.parametrize("evidence,outcome,reason", [
    ([receipt()], "passed", None),
    ([], "unknown", "baseline_evidence_insufficient"),
    ([receipt(output="Ran 0 tests in 0s\nOK")], "unknown", "baseline_zero_tests"),
    ([receipt(output="usage: python\noptions:\n")], "unknown", "baseline_help_only"),
    ([receipt(started_at=None)], "unknown", "baseline_evidence_insufficient"),
    ([receipt(status="timed_out")], "unknown", "baseline_evidence_insufficient"),
    ([receipt(command_hash="f" * 64)], "unknown", "baseline_evidence_insufficient"),
    ([receipt(code=127, output="No such file or directory")], "environment_blocked", "environment_command_unavailable"),
    ([receipt(code=1, output="FAILED (failures=1)\nAssertionError: wrong")], "preexisting_failure", "baseline_actual_assertion_failure"),
    ([receipt(code=1, output="opaque nonzero")], "unknown", "baseline_failure_unclassified"),
])
def test_full_receipt_semantics(evidence, outcome, reason):
    value = classify_baseline(baseline_contract(), "a" * 64, evidence, "b" * 64)
    assert value.outcome == outcome
    assert not reason or reason in value.reason_codes


def test_expected_failure_needs_exact_target_output_control_and_plan_hash():
    commands = [{"command_id": "reproduce", "argv": ["python", "-m", "unittest", "tests.test_bug.Test.test_bug"], "working_directory": "."}, COMMANDS[0]]
    expected = BaselineExpectation(issue_url="https://github.com/example/another/issues/42",
        test_node="tests.test_bug.Test.test_bug", failure_literal="AssertionError: actual regression")
    plan_command = PlanCommand("reproduce", "Reproduce actual issue", tuple(commands[0]["argv"]), baseline_expectation=expected.model_dump(mode="json"))
    assert plan_command.hash_payload() != replace(plan_command, baseline_expectation=None).hash_payload()
    value = baseline_contract(commands)
    value["expected_failures"] = {"reproduce": expected.model_dump(mode="json")}
    VerificationEnvironmentContract.model_validate_json(json.dumps(value))
    fail = receipt(commands[0], code=1, output="tests.test_bug.Test.test_bug\nAssertionError: actual regression\nFAILED (failures=1)")
    got = classify_baseline(value, "a" * 64, [fail, receipt()], "b" * 64)
    assert got.outcome == "expected_issue_reproduction"
    assert classify_baseline(value, "a" * 64, [fail, receipt(code=1, output="unrelated")], "b" * 64).outcome == "unknown"
    assert classify_baseline(value, "a" * 64, [receipt(commands[0]), receipt()], "b" * 64).outcome == "no_change"
    value["expected_failures"]["tests"] = expected.model_dump(mode="json")
    with pytest.raises(ValueError):
        VerificationEnvironmentContract.model_validate_json(json.dumps(value))


@pytest.mark.parametrize("script", ["eslint --help", "tsc --version", "echo passed"])
def test_package_manager_cannot_turn_diagnostic_or_echo_into_a_passing_check(script):
    commands = [{"command_id": "lint", "argv": ["npm", "run", "lint"], "working_directory": "."}]
    value = contract({"package.json": json.dumps({"scripts": {"lint": script}})}, commands, "node").model_dump(mode="json")
    assert value["admission"] == "static_admitted"
    verdict = classify_baseline(value, "a" * 64, [receipt(commands[0], output="Tool diagnostic output")], "b" * 64)
    assert verdict.outcome == "unknown"


def test_named_script_running_tests_needs_positive_test_execution_evidence():
    commands = [{"command_id": "quality", "argv": ["npm", "run", "quality"], "working_directory": "."}]
    value = contract({"package.json": json.dumps({"scripts": {"quality": "vitest run"}})}, commands, "node").model_dump(mode="json")
    verdict = classify_baseline(value, "a" * 64, [receipt(commands[0], output="Started runner successfully")], "b" * 64)
    assert verdict.outcome == "unknown"
    assert "baseline_evidence_insufficient" in verdict.reason_codes
    assert classify_baseline(value, "a" * 64, [receipt(commands[0], output="Tests 4 passed")], "b" * 64).outcome == "passed"


@pytest.mark.parametrize("directory,leaf", [("tools#quality", "node --version"), ("tools space", "echo success"), ("tools", "")])
def test_cwd_edges_and_unusual_directory_names_cannot_hide_diagnostic_or_empty_checks(directory, leaf):
    commands = [{"command_id": "quality", "argv": ["npm", "run", "quality"], "working_directory": "."}]
    script = f"cd '{directory}'" + (" && " + leaf if leaf else "")
    value = contract({"package.json": json.dumps({"scripts": {"quality": script}}), directory + "/README.md": "data"}, commands, "node").model_dump(mode="json")
    assert value["admission"] == "static_admitted"
    got = classify_baseline(value, "a" * 64, [receipt(commands[0], output="zero exit")], "b" * 64)
    assert got.outcome == "unknown"


def test_new_migration_preserves_real_v38_rows_and_backup(tmp_path, monkeypatch):
    import app.migrations.versions as registry
    from app.database import Database
    from app.workbench import Workbench
    from tests.test_agent_v2_orchestrator import prepared
    with monkeypatch.context() as patch:
        patch.setattr(registry, "MIGRATIONS", tuple(m for m in registry.MIGRATIONS if m.revision < "0039"))
        db, settings, task, _, _ = prepared(tmp_path)
        with db.session() as session:
            wb = Workbench(session, settings.artifact_root)
            old = [(e.id, e.record_hash) for e in wb.history(task)]
            plans = [(p.id, p.record_hash, p.content_hash) for p in session.scalars(select(PlanVersion).where(PlanVersion.task_id == task))]
        backup = tmp_path / "v38-backup.db"
        with sqlite3.connect(db.engine.url.database) as src, sqlite3.connect(backup) as dst:
            src.backup(dst)
    assert db.create_schema().applied == ("0039_environment_contracts", "0040_deepseek_reviews", "0041_native_model_tools", "0042_agent_response_evidence", "0043_implementation_progress", "0044_native_read_batches")
    with db.session() as session:
        assert [(e.id, e.record_hash) for e in Workbench(session, settings.artifact_root).history(task)] == old

        assert [(p.id, p.record_hash, p.content_hash) for p in session.scalars(select(PlanVersion).where(PlanVersion.task_id == task))] == plans
        assert session.connection().exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert not session.connection().exec_driver_sql("PRAGMA foreign_key_check").all()
    restored = Database("sqlite:///" + str(backup))
    assert restored.create_schema().applied == ("0039_environment_contracts", "0040_deepseek_reviews", "0041_native_model_tools", "0042_agent_response_evidence", "0043_implementation_progress", "0044_native_read_batches")
    with restored.session() as session:
        assert [(e.id, e.record_hash) for e in Workbench(session, settings.artifact_root).history(task)] == old


def test_admission_recomputes_full_chain_and_rejects_receipt_or_source_change():
    from types import SimpleNamespace
    from app.environment_engine.admission import require_environment_evidence
    from app.environment_engine.contracts import ToolchainPreparationReceipt, ExecutableReceipt
    from app.workbench import WorkbenchError
    values = {}
    def put(value):
        digest = content_hash(value)
        values[digest] = value
        return digest
    contract = baseline_contract()
    contract_hash = put(contract)
    spec = {"environment_contract": contract}
    spec_hash = put(spec)
    dependency_hash = put({"plan_hash": spec_hash})
    prepared = ToolchainPreparationReceipt(contract_hash=contract_hash, dependency_plan_hash=spec_hash,
        dependency_result_hash=dependency_hash, target_platform="linux/arm64", image_digest="sha256:" + "e" * 64,
        policy_hash="f" * 64, status="prepared", executables=(ExecutableReceipt(command_id="tests",
            argv=tuple(COMMANDS[0]["argv"]), working_directory=".", requested_executable="python",
            executable="/dependencies/output/venv/bin/python", status="available"),))
    prepared_hash = put(prepared.model_dump(mode="json"))
    inspection = {"command_evidence": [receipt()]}
    verdict = classify_baseline(contract, prepared_hash, inspection["command_evidence"], content_hash(inspection))
    baseline = {"version": "execution-baseline-v2", "environment_contract_hash": contract_hash,
        "preparation_receipt_hash": prepared_hash, "environment_spec_hash": spec_hash,
        "dependency_result_hash": dependency_hash, "inspection": inspection,
        "baseline_verdict": verdict.model_dump(mode="json"), "entrypoint_evidence_hash": "a" * 64}
    attempt = SimpleNamespace(task_id="task", snapshot_inputs_hash="a" * 64, base_commit_sha="c" * 40,
        repository_archive_hash="b" * 64, runner_image_digest="sha256:" + "e" * 64, sandbox_policy_hash="f" * 64)
    authorization = SimpleNamespace(payload={"plan_hash": "d" * 64, "environment": {"verification_contract_hash": contract_hash}})
    assert require_environment_evidence(SimpleNamespace(read=values.__getitem__), attempt, authorization, baseline)["contract_hash"] == contract_hash
    for changed in ("source", "receipt", "commands", "verdict"):
        copy = deepcopy(baseline)
        modified_attempt = SimpleNamespace(**vars(attempt))
        if changed == "source":
            modified_attempt.repository_archive_hash = "0" * 64
        if changed == "receipt":
            copy["preparation_receipt_hash"] = put({**prepared.model_dump(mode="json"), "target_platform": "linux/amd64"})
        if changed == "commands":
            copy["inspection"]["command_evidence"][0]["command_hash"] = "0" * 64
        if changed == "verdict":
            copy["baseline_verdict"]["outcome"] = "expected_issue_reproduction"
        with pytest.raises(WorkbenchError):
            require_environment_evidence(SimpleNamespace(read=values.__getitem__), modified_attempt, authorization, copy)


def test_old_task_spec_keeps_exact_serialization_without_null_environment_field():
    from tests.test_agent_v2_tools import tool_spec
    from app.agent_v2.contracts import TaskSpec
    spec = tool_spec()
    old = spec.model_dump(mode="json")
    assert "environment_evidence" not in old
    assert TaskSpec.model_validate_json(json.dumps(old)).model_dump(mode="json") == old
    new = spec.model_copy(update={"environment_evidence": {"contract_hash": "a" * 64}})
    assert new.model_dump(mode="json")["environment_evidence"] == {"contract_hash": "a" * 64}
    assert content_hash(new.model_dump(mode="json")) != content_hash(old)
