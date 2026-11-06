"""Offline capacity regressions. Scripted provider/tools, never paid acceptance."""
import asyncio
import copy
import json
import pytest
from pathlib import Path

from app.agent_v2.journal import AgentJournal
from app.provenance import canonical_json, content_hash
from app.workbench import Workbench
from tests.test_native_read_batches import ReadGateway, setup, inspect, drain, reads

SAVED_PATH = Path(__file__).parents[1] / "docs/agent-harness-reset-v1/read-batch-scheduler-v1/plan14-execution/artifacts/f941ae3218fd1994b1e683e8ab0a57d4cc788a6ed4700624bd1d05f0fb8feee3.json"


def saved_response():
    envelope = json.loads(SAVED_PATH.read_text())
    assert content_hash(envelope) == SAVED_PATH.stem
    return {"choices": [{"finish_reason": c["finish_reason"], "message": {"role": c["role"],
        "tool_calls": copy.deepcopy(c["tool_calls"])}} for c in envelope["choices"]], "usage": envelope["usage"]}


def test_saved_plan14_maximum_pages_reach_preflight_without_widening_budget(tmp_path):
    values = setup(tmp_path, saved_response())
    assert asyncio.run(values[4].run_once()).state == "succeeded"
    assert inspect(values)[0].state == "tools"
    assert not values[-1].calls
    drain(values[5])
    cp, events, _ = inspect(values)
    assert cp.state == "ready" and cp.usage["reads"] == 8
    results = [e for e in events if e.kind == "agent_v2_tool_result"]
    assert [e.payload["provider_call_id"] for e in results] == [c["id"] for c in saved_response()["choices"][0]["message"]["tool_calls"]]
    assert [len(e.payload["call_ids"]) for e in events if e.kind == "agent_v2_read_sub_batch_completed"] == [4, 4]
    with values[0].session() as session:
        journal = AgentJournal(Workbench(session, values[1].artifact_root), values[2], values[3])
        assert journal.spec().limits.output_bytes == 500000
        certificate = next(e for e in journal.events() if e.kind == "agent_v2_read_capacity")
        assert journal.wb.read(certificate.payload["artifact_id"])["status"] == "succeeded"


def test_final_failure_receipt_is_the_object_charged_and_persisted(tmp_path):
    values = setup(tmp_path, reads(5), gateway=ReadGateway(fail_at=1))
    asyncio.run(values[4].run_once()); drain(values[5])
    cp, events, _ = inspect(values)
    with values[0].session() as session:
        journal = AgentJournal(Workbench(session, values[1].artifact_root), values[2], values[3])
        model_bytes = sum(len(journal.wb.artifacts.read_bytes(e.payload["artifact_id"]))
            for e in events if e.kind == "agent_v2_model_response")
        tool_bytes = sum(len(journal.wb.artifacts.read_bytes(e.payload["artifact_id"]))
            for e in events if e.kind == "agent_v2_tool_result")
        metadata_bytes = sum(e.payload["bytes"] for e in events
            if e.kind == "agent_v2_output_charge" and e.payload["role"] != "tool_result")
        assert cp.usage["output_bytes"] == model_bytes + tool_bytes + metadata_bytes


@pytest.mark.parametrize("bad", ["bytes", "page", "hash", "version", "source", "requests"])
def test_unverifiable_capacity_is_rejected_before_any_read(tmp_path, bad):
    class Forged(ReadGateway):
        async def preflight_read_batch(self, *args, **kwargs):
            proof = await super().preflight_read_batch(*args, **kwargs)
            if bad == "bytes": proof["checked"][-1]["data_json_bytes"] = 2
            if bad == "page": proof["checked"][-1]["page_bytes"] = 16001
            if bad == "hash": proof["checked"][-1]["data_hash"] = "invalid"
            if bad == "version": proof["version"] = "native-read-batch-preflight-v2"
            if bad == "source": proof["source_hash"] = "b" * 64
            if bad == "requests": proof["requests_hash"] = "b" * 64
            return proof
    values = setup(tmp_path, reads(8), gateway=Forged())
    asyncio.run(values[4].run_once()); drain(values[5])
    assert inspect(values)[0].state == "blocked" and not values[-1].calls


def test_true_output_shortage_after_attestation_dispatches_zero_reads(tmp_path):
    from tests.test_native_read_batch_boundaries import seed_reserved_usage
    values = setup(tmp_path, reads(5))
    seed_reserved_usage(values, {"output_bytes": 450000})
    asyncio.run(values[4].run_once()); drain(values[5])
    cp, events, _ = inspect(values)
    assert cp.state == "blocked" and cp.usage["reads"] == 5
    assert cp.usage["output_bytes"] > 450000 and not values[-1].calls
    assert any(e.kind == "agent_v2_read_capacity" and e.payload["status"] == "rejected" for e in events)


def test_attested_data_cannot_change_before_result_and_observed_truth_is_preserved(tmp_path):
    class Changed(ReadGateway):
        async def execute(self, *args, **kwargs):
            value = await super().execute(*args, **kwargs)
            value["result"]["data"]["text"] = "different source"
            return value
    values = setup(tmp_path, reads(5), gateway=Changed())
    asyncio.run(values[4].run_once()); drain(values[5])
    cp, events, _ = inspect(values)
    assert cp.state == "blocked" and len(values[-1].calls) == 1
    result_event = next(e for e in events if e.kind == "agent_v2_tool_result")
    with values[0].session() as session:
        journal = AgentJournal(Workbench(session, values[1].artifact_root), values[2], values[3])
        value = journal.wb.read(result_event.payload["artifact_id"])
        assert value["status"] == "unknown"
        assert journal.wb.read(value["data"]["observed_result_artifact"])["data"]["text"] == "different source"


@pytest.mark.parametrize("text,expected", [("\x01" * 16000, "blocked"),
    ('\\"\n\t' * 1000, "ready"), ("中文🙂" * 3000, "ready")], ids=["controls", "JSON_escapes", "UTF8"])
def test_escaped_and_utf8_pages_fit_only_with_complete_verified_bound(tmp_path, text, expected):
    gateway = ReadGateway(); gateway.text = text
    values = setup(tmp_path, reads(8, max_bytes=16000), gateway=gateway)
    asyncio.run(values[4].run_once()); drain(values[5])
    cp, events, _ = inspect(values)
    assert cp.state == expected
    if cp.state == "blocked":
        assert not gateway.calls  # Genuinely unaffordable escaped source.
    else:
        assert cp.state == "ready" and len(gateway.calls) == 1  # Seven exact requests reuse the real first receipt.
        assert len([e for e in events if e.kind == "agent_v2_tool_result"]) == 8
        with values[0].session() as session:
            journal = AgentJournal(Workbench(session, values[1].artifact_root), values[2], values[3])
            for event in (e for e in events if e.kind == "agent_v2_tool_result"):
                d = journal.wb.read(event.payload["artifact_id"])["data"]
                end = d["offset"] + len(d["text"].encode())
                assert d["next_offset"] == (end if end < d["total_bytes"] else None)
                assert text.encode()[d["offset"]:end] == d["text"].encode()


def test_expired_execution_stops_but_can_commit_the_real_prefix_and_diagnostic(tmp_path, monkeypatch):
    values = setup(tmp_path, reads(8, offset=0))
    asyncio.run(values[4].run_once())
    asyncio.run(values[5].run_once()); asyncio.run(values[5].run_once())
    assert len(values[-1].calls) == 1
    monkeypatch.setattr(AgentJournal, "elapsed_seconds", lambda _: 2000)
    drain(values[5])
    cp, events, _ = inspect(values)
    assert cp.state == "blocked" and not cp.pending_calls
    assert len(values[-1].calls) == 1
    assert any(e.kind == "agent_v2_read_batch_stopped" for e in events)


def test_additive_capacity_migration_preserves_legacy_and_guards_exact_charge(tmp_path, monkeypatch):
    from app.migrations import versions
    from sqlalchemy.exc import IntegrityError
    with monkeypatch.context() as patch:
        patch.setattr(versions, "MIGRATIONS", tuple(m for m in versions.MIGRATIONS if m.revision < "0046_read_capacity"))
        values = setup(tmp_path, reads(5))
    before = [e.record_hash for e in inspect(values)[1]]
    assert values[0].create_schema().applied == ("0046_read_capacity",)
    assert before == [e.record_hash for e in inspect(values)[1]]
    with values[0].session() as session:
        journal = AgentJournal(Workbench(session, values[1].artifact_root), values[2], values[3])
        aid = journal.wb.artifact({"unmetered": True})
        with pytest.raises(IntegrityError, match="Invalid exact output charge"):
            journal.append("output_charge", {"artifact_id": aid, "bytes": 1, "role": "tool_result",
                "contract": "native-read-capacity-v1"}, key="forged-byte-count")
        session.rollback()


def test_legacy_preflight_versions_remain_readable_but_cannot_issue_capacity_certificate(tmp_path):
    from app.agent_v2.read_batches import parse_preflight
    from app.agent_v2.read_capacity import certificate
    values = setup(tmp_path, reads(5)); asyncio.run(values[4].run_once())
    with values[0].session() as session:
        journal = AgentJournal(Workbench(session, values[1].artifact_root), values[2], values[3])
        spec = journal.spec()
    proof = asyncio.run(values[-1].preflight_read_batch(spec, inspect(values)[0].pending_calls,
        source_hash=spec.binding.archive_hash, snapshot=None, input_hash="a" * 64))
    for version in ("native-read-batch-preflight-v1", "native-read-batch-preflight-v2"):
        legacy = copy.deepcopy(proof); legacy["version"] = version
        for check in legacy["checked"]:
            for key in ("data_json_bytes", "data_hash", "page_bytes"): check.pop(key)
            if version.endswith("v1"): check.pop("availability")
        assert parse_preflight(legacy)["version"] == version
        with pytest.raises(ValueError, match="read_capacity_proof_version_invalid"):
            certificate({}, legacy, batch_artifact="b" * 64, proof_artifact="c" * 64, remaining=500000)


def test_frozen_plan14_projection_and_raw_source_pages_recover_under_original_limits(tmp_path):
    from tests.capacity_fixture import FrozenGateway
    from tests.capacity_projection import reconstruction
    from app.agent_v2.loop import issued_artifacts, prompt_for
    values = setup(tmp_path, saved_response(), gateway=FrozenGateway())
    asyncio.run(values[4].run_once()); drain(values[5])
    with values[0].session() as session:
        journal = AgentJournal(Workbench(session, values[1].artifact_root), values[2], values[3])
        receipts = [journal.wb.read(e.payload['artifact_id']) for e in journal.events() if e.kind == 'agent_v2_tool_result']
        for r in receipts:
            aid = r['data']['source_page_artifact']
            assert journal.wb.artifacts.read_bytes(aid) == r['data']['text'].encode()
            assert aid in issued_artifacts(journal)
        report, prompt, _ = reconstruction(receipts)
        assert report['returned_source_bytes'] == 68338
        assert report['inline_source_bytes'] > 0
        assert all(r['prompt_inline_source_bytes'] > 0 and r['read_artifact_recovers_entire_returned_page'] for r in report['rows'])
        assert report['focused_artifact_recovery_rounds_total'] <= 12
        assert prompt['approved_task_input']['partial'] is True
        assert report['native_input_bound'] <= 32000
        actual = prompt_for(journal)
        assert [r['call_id'] for r in actual['feedback']] == [r['call_id'] for r in receipts]
        # Meter every actual persisted output object, including raw page bodies.
        events = journal.events()
        model_bytes = sum(len(journal.wb.artifacts.read_bytes(e.payload['artifact_id'])) for e in events if e.kind == 'agent_v2_model_response')
        charges = [e for e in events if e.kind == 'agent_v2_output_charge']
        assert all(e.payload['bytes'] == len(journal.wb.artifacts.read_bytes(e.payload['artifact_id'])) for e in charges)
        assert journal.checkpoint().usage['output_bytes'] == model_bytes + sum(e.payload['bytes'] for e in charges)
        assert len([e for e in charges if e.payload['role'] == 'source_page']) == 8


def test_raw_source_artifact_is_deduplicated_and_does_not_issue_foreign_refs(tmp_path):
    from app.agent_v2.loop import issued_artifacts
    values = setup(tmp_path, reads(8))
    asyncio.run(values[4].run_once()); drain(values[5])
    with values[0].session() as session:
        j = AgentJournal(Workbench(session, values[1].artifact_root), values[2], values[3])
        events = j.events()
        assert len([e for e in events if e.kind == 'agent_v2_output_charge' and e.payload['role'] == 'source_page']) == 1
        foreign = j.wb.artifact({'unissued': True})
        assert foreign not in issued_artifacts(j)
        assert len(values[-1].calls) == 1
        assert j.checkpoint().usage['reads'] == 8


def test_issued_raw_source_page_is_recovered_by_existing_budgeted_tool(tmp_path):
    from tests.capacity_fixture import FrozenGateway, CONTENTS
    from tests.test_agent_native_tools import response
    values = setup(tmp_path, saved_response(), gateway=FrozenGateway())
    asyncio.run(values[4].run_once()); drain(values[5])
    with values[0].session() as session:
        j = AgentJournal(Workbench(session, values[1].artifact_root), values[2], values[3])
        result = j.wb.read(next(e for e in j.events() if e.kind == 'agent_v2_tool_result').payload['artifact_id'])
        aid = result['data']['source_page_artifact']; path = result['data']['path']
    values[6].responses.append(response('read_artifact', {'artifact_id': aid, 'offset': 0, 'max_bytes': 16000}))
    assert asyncio.run(values[4].run_once()).state == 'succeeded'
    assert asyncio.run(values[5].run_once()).state == 'succeeded'
    with values[0].session() as session:
        j = AgentJournal(Workbench(session, values[1].artifact_root), values[2], values[3])
        event = next(e for e in reversed(j.events()) if e.kind == 'agent_v2_tool_result')
        recovered = j.wb.read(event.payload['artifact_id'])
        assert event.payload['name'] == 'read_artifact' and recovered['data']['text'] == CONTENTS[path]
        assert j.checkpoint().usage['reads'] == 9 and len(values[-1].calls) == 8


def test_gateway_cannot_smuggle_a_foreign_source_artifact_reference(tmp_path):
    class Foreign(ReadGateway):
        async def execute(self, *args, **kw):
            value = await super().execute(*args, **kw)
            value['result']['data']['source_page_artifact'] = 'b' * 64
            return value
    values = setup(tmp_path, reads(8), gateway=Foreign())
    asyncio.run(values[4].run_once()); drain(values[5])
    assert inspect(values)[0].state == 'blocked' and len(values[-1].calls) == 1
    with values[0].session() as session:
        from app.agent_v2.loop import issued_artifacts
        j = AgentJournal(Workbench(session, values[1].artifact_root), values[2], values[3])
        assert 'b' * 64 not in issued_artifacts(j)


def test_saved_plan14_envelope_carries_forward_historical_budget_without_refund(tmp_path):
    """Synthetic counters only; never restores or edits the real Plan14 session."""
    from tests.capacity_fixture import FrozenGateway
    from tests.test_native_read_batch_boundaries import seed_reserved_usage
    values = setup(tmp_path, saved_response(), gateway=FrozenGateway())
    # Saved response Usage=8946/2201; final saved charged totals=46008/4560.
    # The canonical new fixture response is 1933 bytes, as in the exported replay.
    seed_reserved_usage(values, {'model_calls': 2, 'reads': 5, 'output_bytes': 35526 - 1933,
        'input_tokens': 46008 - 8946, 'output_tokens': 4560 - 2201})
    assert asyncio.run(values[4].run_once()).state == 'succeeded'
    drain(values[5])
    with values[0].session() as session:
        j = AgentJournal(Workbench(session, values[1].artifact_root), values[2], values[3])
        cp = j.checkpoint(); assert cp.state == 'ready'
        assert cp.usage['model_calls'] == 3 and cp.usage['reads'] == 13
        assert cp.usage['input_tokens'] == 46008 and cp.usage['output_tokens'] == 4560
        charges = sum(e.payload['bytes'] for e in j.events() if e.kind == 'agent_v2_output_charge')
        assert cp.usage['output_bytes'] == 35526 + charges < 500000
        assert len(values[-1].calls) == 8


def test_raw_page_charge_and_receipt_checkpoint_rollback_together_on_persist_failure(tmp_path, monkeypatch):
    from app.agent_v2 import read_capacity
    from app.agent_v2.loop import issued_artifacts, schedule
    values = setup(tmp_path, reads(8))
    asyncio.run(values[4].run_once()); asyncio.run(values[5].run_once())
    original = read_capacity.store
    def failed_store(journal, value, usage, *, role):
        if role == 'tool_result': raise RuntimeError('injected crash before final receipt commit')
        return original(journal, value, usage, role=role)
    with monkeypatch.context() as patch:
        patch.setattr(read_capacity, 'store', failed_store)
        assert asyncio.run(values[5].run_once()).state == 'failed'
    with values[0].session() as session:
        j = AgentJournal(Workbench(session, values[1].artifact_root), values[2], values[3])
        assert not any(e.kind == 'agent_v2_output_charge' and e.payload['role'] == 'source_page' for e in j.events())
        assert not any(e.kind == 'agent_v2_tool_result' for e in j.events())
        assert schedule(j) is None
        session.commit()
    drain(values[5])
    cp, events, _ = inspect(values)
    assert cp.state == 'blocked' and cp.usage['reads'] == 8 and len(values[-1].calls) == 1
    stopped = next(e for e in events if e.kind == 'agent_v2_read_batch_stopped')
    assert len(stopped.payload['unknown_calls']) == 1 and len(stopped.payload['not_executed']) == 7
    assert stopped.payload['actual_receipts'] == []
