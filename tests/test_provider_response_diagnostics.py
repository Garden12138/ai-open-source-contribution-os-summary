"""Offline diagnostics contracts, never counted as a native model success."""
import asyncio
from dataclasses import FrozenInstanceError
import json

import pytest
from fastapi.testclient import TestClient

from app.agent_v2.journal import AgentJournal
from app.agent_v2.loop import AgentModelWorker
from app.agent_v2.provider_failures import provider_failure_receipt
from app.api import create_app
from app.providers.contracts import ProviderContractError, ProviderRunError, ProviderUsage
from app.providers.nvidia_nim import STRUCTURED_OUTPUT_TOOL_NAME, _completion
from app.providers.response_diagnostics import CompletionResponseFacts, completion_response_facts
from app.workbench import Workbench
from tests.test_agent_v2_loop import fixture

PRIVATE = "PRIVATE_REASONING_DIAGNOSTIC_CANARY"


def response():
    return {"choices": [{"finish_reason": "stop", "message": {"content": "", "reasoning_content": PRIVATE,
        "tool_calls": [{"type": "function", "function": {"name": STRUCTURED_OUTPUT_TOOL_NAME, "arguments": '{"ok":true}'}}]}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10}}


@pytest.mark.parametrize("case,reason", [("choices", "choices_invalid"), ("usage", "usage_unverified"),
    ("finish", "finish_unverified"), ("length", "output_truncated"), ("content", "final_content_missing")])
def test_failed_completion_retains_only_allowlisted_response_facts(case, reason):
    data = response()
    if case == "choices": data["choices"] = []
    elif case == "usage": data.pop("usage")
    elif case == "finish": data["choices"][0]["finish_reason"] = PRIVATE
    elif case == "length": data["choices"][0]["finish_reason"] = "length"
    else: data["choices"][0]["message"].pop("tool_calls")
    with pytest.raises(ProviderRunError) as caught:
        _completion(data, error_prefix="deepseek", provider_label="DeepSeek")
    error = caught.value
    assert not error.retryable and error.diagnostic.reason == reason
    assert (error.usage is None) == (case == "usage")
    wire = error.diagnostic.to_wire()
    assert set(wire) == {"version", "reason", "choice_count", "finish", "final_text_present", "tool_calls_present", "reasoning_present", "tool_envelope", "tool_call_count"}
    assert wire['version'] == 'completion-response-facts-v2'
    assert PRIVATE not in json.dumps(wire)
    if case == "finish": assert wire["finish"] == "other"


@pytest.mark.parametrize("value", [{}, {"choices": None}, {"choices": [None]}, {"choices": [{"finish_reason": [], "message": []}]},
    {"choices": [response()["choices"][0]] * 1025}])
def test_malformed_or_oversized_fields_cannot_leak_into_facts(value):
    facts = completion_response_facts(value, "choices_invalid")
    assert facts.choice_count in (None, 1) and not facts.final_text_present
    assert PRIVATE not in json.dumps(facts.to_wire())


@pytest.mark.parametrize("changes", [{"reason": PRIVATE}, {"finish": PRIVATE}, {"reason": []}, {"choice_count": True},
    {"choice_count": -1}, {"choice_count": 1025}, {"reasoning_present": "false"}])
def test_facts_are_immutable_strict_allowlisted_values(changes):
    values = dict(reason="choices_invalid", choice_count=0, finish="missing", final_text_present=False,
        tool_calls_present=False, reasoning_present=False)
    with pytest.raises(ValueError): CompletionResponseFacts(**{**values, **changes})
    facts = CompletionResponseFacts(**values)
    with pytest.raises(FrozenInstanceError): facts.finish = "stop"
    with pytest.raises(ProviderContractError): ProviderRunError("model_invalid", "Invalid response", retryable=False, diagnostic={"raw": PRIVATE})


@pytest.mark.parametrize("usage,status", [(None, "unreported"), (ProviderUsage(input_tokens=20, output_tokens=10), "reported_within_reservation"),
    (ProviderUsage(input_tokens=200, output_tokens=10), "reported_exceeds_reservation"),
    (ProviderUsage(input_tokens=20.5, output_tokens=10), "reported_invalid")])
def test_failed_usage_is_separate_from_retained_reservation(usage, status):
    error = ProviderRunError("model_invalid", "Invalid response", retryable=False, usage=usage)
    receipt = provider_failure_receipt(error, input_bound=100, output_limit=40)
    assert receipt["usage_status"] == status and receipt["reservation"] == {"input_tokens": 100, "output_tokens": 40}
    assert receipt["reservation_retained"] and receipt["response_facts"] is None
    if usage is None or status == "reported_invalid": assert receipt["reported_usage"] is None


@pytest.mark.parametrize("provider", ["deepseek", "nvidia_nim", "minimax"])
def test_successful_completion_contract_is_unchanged(provider):
    content, usage = _completion(response(), error_prefix=provider)
    assert content == '{"ok":true}' and usage["input_tokens"] == 20 and usage["output_tokens"] == 10
    assert PRIVATE not in content


@pytest.mark.parametrize("reported", [False, True])
def test_native_worker_failure_api_keeps_budget_terminal_state_and_no_retry(tmp_path, reported):
    db, settings, task, _, _, _, _, _, _, sid = fixture(tmp_path)
    captured = []
    class Adapter:
        capability = "structured_tools"
        async def decide(self, prompt, *, request_id, max_output_tokens):
            captured.append(max_output_tokens)
            raise ProviderRunError("deepseek_completion_invalid", "No verified final output", retryable=False,
                usage=ProviderUsage(input_tokens=20, output_tokens=10) if reported else None,
                diagnostic=completion_response_facts(response(), "final_content_missing"))
    worker = AgentModelWorker(db, settings, lambda *_: Adapter(), worker_id="diagnostic-model")
    assert asyncio.run(worker.run_once()).state == "succeeded"
    assert asyncio.run(worker.run_once()) is None
    with db.session() as session:
        journal = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        checkpoint = journal.checkpoint()
        rejected = next(e for e in journal.events() if e.kind == "agent_v2_model_rejected")
        receipt = rejected.payload["provider_failure_receipt"]
        assert checkpoint.state == "blocked" and captured == [4096]
        assert checkpoint.usage["model_calls"] == 1 and checkpoint.usage["output_tokens"] == receipt["reservation"]["output_tokens"] == 4096
        assert checkpoint.usage["input_tokens"] == receipt["reservation"]["input_tokens"]
        assert not checkpoint.usage.get("protocol_repairs") and not checkpoint.usage.get("provider_retries")
        assert receipt["reported_usage"] == ({"input_tokens": 20, "output_tokens": 10} if reported else None)
        assert journal.spec().limits.output_tokens == 32000 and journal.spec().limits.model_calls == 24
        before = checkpoint
        assert journal.control("continue", checkpoint.revision) == before
        assert not journal.wb.latest(task, "automatic_change_accepted")
        assert len([e for e in journal.wb.history(task) if e.kind == "execution_authorized"]) == 1
    with TestClient(create_app(settings)) as client:
        view = client.get(f"/api/v1/tasks/{task}/workbench/runtime").json()
    actual = next(e["payload"]["provider_failure_receipt"] for e in view["timeline"] if e["kind"] == "agent_v2_model_rejected")
    assert actual == receipt and PRIVATE not in json.dumps(view)
    db.close()
