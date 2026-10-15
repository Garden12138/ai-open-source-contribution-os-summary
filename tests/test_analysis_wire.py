from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest

from app.config import Settings
from app.database import Database
from app.jobs import JobService
from app.model_settings import ModelSettingsService, configure_bound_worker, job_profile
from app.providers import ProviderContractError, validate_structured_analysis
from app.providers.analysis_schema import ANALYSIS_OUTPUT_SCHEMA
from app.providers.analysis_wire import (
    PAIRED_FIELDS, PAIRED_ANALYSIS_ADAPTER_VERSION,
    paired_analysis_schema, decode_paired_analysis,
)
from app.providers.codex_cli import CodexExecInvocation, _bind_evidence_id_enums
from app.providers.contracts import ProviderRunError, ProviderStage
from app.providers.studio import StudioRunner, profile_identity
from tests.test_analysis_schema import cited_analysis_payload
from tests.test_model_settings import configure


def paired_payload():
    value = deepcopy(cited_analysis_payload())
    for field in PAIRED_FIELDS:
        citations = value["citation_map"].pop(field)
        value[field] = [{"value": statement, "evidence_ids": row} for statement, row in zip(value[field], citations, strict=True)]
    return value


def test_pairs_preserve_every_statement_and_citation_with_optional_empty_lists():
    original = cited_analysis_payload()
    paired = paired_payload()
    assert decode_paired_analysis(paired) == original
    # An empty optional list produces exactly zero citation rows.
    paired["maintainer_questions"] = []
    decoded = decode_paired_analysis(paired)
    assert decoded["maintainer_questions"] == decoded["citation_map"]["maintainer_questions"] == []
    validate_structured_analysis(decoded, allowed_evidence_ids=("issue", "repository", "rule_score"))


def test_wire_schema_preserves_frozen_evidence_enums_and_original_schema():
    bound = _bind_evidence_id_enums(ANALYSIS_OUTPUT_SCHEMA, ("issue", "repository", "rule_score"))
    before = deepcopy(bound)
    schema = paired_analysis_schema(bound)
    assert bound == before
    for field in PAIRED_FIELDS:
        item = schema["properties"][field]["items"]
        assert item["properties"]["value"] == bound["properties"][field]["items"]
        assert item["properties"]["evidence_ids"] == bound["properties"]["citation_map"]["properties"][field]["items"]
        assert field not in schema["properties"]["citation_map"]["required"]
        assert item["additionalProperties"] is False


@pytest.mark.parametrize("mutation", [
    lambda v: v["fit_reasons"][0].pop("evidence_ids"),
    lambda v: v["fit_reasons"][0].update(evidence_ids=[]),
    lambda v: v["fit_reasons"][0].update(extra="ignore validation"),
    lambda v: v["citation_map"].update(fit_reasons=[["issue"]]),
    lambda v: v.update(acceptance_criteria=["An unpaired statement"]),
])
def test_missing_or_ambiguous_citations_fail_without_padding_or_reassignment(mutation):
    value = paired_payload()
    mutation(value)
    with pytest.raises(ProviderRunError) as exc:
        decode_paired_analysis(value)
    assert exc.value.code.startswith("studio_analysis_pairs_")


def test_identical_redundant_citation_rows_are_lossless_but_conflicts_fail():
    value = paired_payload()
    value["citation_map"]["acceptance_criteria"] = deepcopy(cited_analysis_payload()["citation_map"]["acceptance_criteria"])
    assert decode_paired_analysis(value) == cited_analysis_payload()
    value["citation_map"]["acceptance_criteria"].reverse()
    with pytest.raises(ProviderRunError) as exc:
        decode_paired_analysis(value)
    assert exc.value.code == "studio_analysis_pairs_acceptance_criteria_conflicting_rows"


def test_unknown_citations_still_fail_durable_validation():
    value = paired_payload()
    value["acceptance_criteria"][0]["evidence_ids"] = ["invented"]
    decoded = decode_paired_analysis(value)
    with pytest.raises(ProviderContractError):
        validate_structured_analysis(decoded, allowed_evidence_ids=("issue", "repository", "rule_score"))


@pytest.mark.parametrize("mode", ["tools", "json_schema"])
@pytest.mark.parametrize("paired", [False, True])
def test_studio_analysis_sends_paired_schema_and_returns_valid_durable_contract(mode, paired):
    invocation = CodexExecInvocation(
        stage=ProviderStage.ANALYZE, request_id="r1", correlation_id="c1",
        snapshot_id="s1", input_hash="a" * 64, model="MiniMax-M3", prompt="Frozen evidence",
        output_schema=_bind_evidence_id_enums(ANALYSIS_OUTPUT_SCHEMA, ("issue", "repository", "rule_score")),
    )
    profile = {"model":"MiniMax-M3", "connection":{"provider":"minimax"}, "structured_output":mode,
               "max_tokens":4096, "timeout_seconds":30}
    class Broker:
        def issue(self, invocation):
            return SimpleNamespace(base_url="http://gateway", token="task-access")
    def handler(request):
        body = json.loads(request.content)
        schema = body["tools"][0]["function"]["parameters"] if mode == "tools" else body["response_format"]["json_schema"]["schema"]
        if paired:
            assert schema["properties"]["fit_reasons"]["items"]["required"] == ["value", "evidence_ids"]
        else:
            assert schema["properties"]["fit_reasons"]["items"]["type"] == "string"
        assert ("paired-statement" in body["messages"][0]["content"]) == paired
        if paired:
            assert json.dumps(schema["required"], separators=(",", ":")) in body["messages"][0]["content"]
        output = paired_payload() if paired else cited_analysis_payload()
        return httpx.Response(200, json={"choices":[{"message":{"content":json.dumps(output)},"finish_reason":"stop"}], "usage":{"prompt_tokens":20,"completion_tokens":40}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await StudioRunner(profile, broker=Broker(), client=client, paired_analysis=paired).complete(invocation)
            assert json.loads(result.content) == cited_analysis_payload()
            validate_structured_analysis(json.loads(result.content), allowed_evidence_ids=("issue", "repository", "rule_score"))
            assert (result.input_tokens, result.output_tokens) == (20,40)
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['tools','json_schema'])
@pytest.mark.parametrize('stage', [ProviderStage.PLANNING,ProviderStage.CODING,ProviderStage.CHANGE_SET,ProviderStage.REVIEW])
def test_studio_non_analysis_preserves_stage_schema_and_language_instructions(mode,stage):
    from app.conversation_reasoning import ConversationDecision
    schema=ConversationDecision.model_json_schema()
    assert set(schema['required'])=={'reply','action','reads','references'}
    invocation=CodexExecInvocation(stage=stage,request_id='stage-request',correlation_id='task',snapshot_id='snapshot',
        input_hash='a'*64,model='MiniMax-M3',prompt='Follow this stage contract.',output_schema=schema)
    profile={'model':'MiniMax-M3','connection':{'provider':'minimax'},'structured_output':mode,'max_tokens':4096,'timeout_seconds':30}
    class Broker:
        def issue(self,invocation): return SimpleNamespace(base_url='http://gateway',token='task-access')
    def handler(request):
        body=json.loads(request.content); system=body['messages'][0]['content']
        assert 'Copy all evidence citation IDs' not in system
        assert 'Reply in Simplified Chinese' not in system
        assert 'stage-specific schema' in system
        actual=body['tools'][0]['function']['parameters'] if mode=='tools' else body['response_format']['json_schema']['schema']
        assert actual==schema
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'reply':'当前答复','action':'respond','reads':[],'references':[]})},'finish_reason':'stop'}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result=await StudioRunner(profile,broker=Broker(),client=client).complete(invocation)
            assert json.loads(result.content)['action']=='respond'
    asyncio.run(run())


@pytest.mark.parametrize("paired", [False, True])
def test_queued_analysis_keeps_frozen_adapter_and_model_binding(tmp_path, paired):
    db = Database(f"sqlite:///{tmp_path / 'binding.db'}")
    db.create_schema()
    with db.session() as session:
        row = configure(session, provider="minimax", model="MiniMax-M3")
        profile = ModelSettingsService(session).profile(row.id)
        identity = profile_identity(profile, paired_analysis=paired)
        job, _ = JobService(session).enqueue(kind="provider_analysis", idempotency_key="analysis-binding", payload={"expected_provider":identity.hash_payload()})
        assert job_profile(session, job.id)["record_hash"] == profile["record_hash"]
        job_id = job.id
    worker = SimpleNamespace(database=db, provider=None)
    configure_bound_worker(worker, job_id, Settings(), analysis=True)
    assert worker.provider.identity == identity
    assert worker.provider.runner.paired_analysis == paired
    assert identity.adapter_version == (PAIRED_ANALYSIS_ADAPTER_VERSION if paired else "studio-model-v1")
