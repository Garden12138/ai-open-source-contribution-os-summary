"""Retrospective projection of real frozen Issue/Plan bytes, never a live request.

The original binding is namespaced to synthetic IDs: this renderer cannot
authorize or resume Plan14. Data observations come from the offline scheduler;
only their outer authority identifiers are rebound for this isolated rendering.
"""
import copy
import json
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from app.agent_v2.contracts import TaskSpec, ToolResult
from app.agent_v2.native_tools import native_model_transport, project_native_prompt, native_input_bound
from app.agent_v2.tools import artifact_page
from app.provenance import canonical_json, content_hash
from tests.capacity_fixture import RECORDS, CONTENTS


def reconstruction(receipts: list[dict], *, output_delta: int = 250000):
    root = RECORDS / "plan14-execution"
    events = json.loads((root / "evidence-index.json").read_text())["events"]
    artifacts = {}
    for path in (root / "artifacts").glob("*.json"):
        value = json.loads(path.read_text()); assert content_hash(value) == path.stem
        artifacts[path.stem] = value
    original_spec = TaskSpec.model_validate(artifacts[next(e for e in events if e["kind"] == "agent_v2_session")["payload"]["spec_artifact"]])
    binding = original_spec.binding.model_copy(update={
        k: str(uuid5(NAMESPACE_URL, "offline-capacity-projection/" + k))
        for k in ("task_id", "attempt_id", "plan_id")})
    binding = binding.model_copy(update={"authorization_hash": content_hash({"synthetic_only": True, "task": binding.task_id})})
    spec = original_spec.model_copy(update={"binding": binding, "model_transport": native_model_transport()})
    prompt = copy.deepcopy(artifacts[next(e for e in reversed(events) if e["kind"] == "agent_v2_model_started")["payload"]["prompt_artifact"]])
    frozen_input_id = prompt["task_context_artifact"]
    inputs = {**artifacts[frozen_input_id], "binding_hash": content_hash(binding.model_dump())}
    input_id = content_hash(inputs); artifacts[input_id] = inputs
    feedback = []
    for index, receipt in enumerate(receipts):
        value = ToolResult.model_validate(receipt).model_copy(update={
            "call_id": "tool-" + content_hash([binding.task_id, index])[:40],
            "input_hash": content_hash([binding.model_dump(), index, receipt["data"]["path"]])}).model_dump()
        aid = content_hash(value); artifacts[aid] = value
        feedback.append({**value, "receipt_artifact": aid})
    prompt.update(binding=binding.model_dump(), task_context_artifact=input_id, feedback=feedback,
        source_hash=feedback[0]["output_source_hash"], goal=spec.goal, write_paths=spec.write_paths, approved_checks=spec.commands)
    # The saved pre-third-turn frame has stale budgets. Use the already charged
    # historical third response and ADD only new offline tool/evidence costs.
    audit = json.loads((root / 'final-audit.json').read_text())
    remaining = dict(audit['failures'][-1]['failure']['remaining_budget'])
    remaining['reads'] -= len(receipts)
    remaining['output_bytes'] -= output_delta
    assert all(v >= 0 for v in remaining.values())
    prompt['remaining_budget'] = remaining
    prompt['recent_decisions'] = [*prompt.get('recent_decisions', []), {'reply': '', 'outcome': 'continue',
        'tools': [{'name': 'read_file', 'path': r['data']['path']} for r in feedback]}][-4:]

    # Run the real ledger reducer in a memory-only projection over the saved
    # pre-response ledger and newly rebound receipts. No old record is edited.
    from app.agent_v2.progress import POLICY, record_tool, prompt_progress
    from app.agent_v2.contracts import ToolCall
    saved_turn = next(e for e in reversed(events) if e['kind'] == 'agent_v2_model_started')
    rows = [SimpleNamespace(**{**e, 'actor_id': 'orchestrator', 'job_id': e.get('job_id')})
        for e in events if e['kind'] == 'agent_v2_progress' and e['sequence'] < saved_turn['sequence']]
    class Shadow:
        def __init__(self):
            self.wb = SimpleNamespace(read=lambda aid: artifacts[aid], artifact=self.artifact)
        def artifact(self, value):
            aid = content_hash(value); artifacts[aid] = value; return aid
        def events(self): return rows
        def root(self): return SimpleNamespace(payload={'harness_policy': POLICY})
        def lock(self): pass
        def checkpoint(self): return SimpleNamespace(revision='f' * 64, source_hash=prompt['source_hash'])
        def append(self, kind, payload, **kw):
            rows.append(SimpleNamespace(kind='agent_v2_' + kind, payload=payload,
                actor_id='orchestrator', record_hash=content_hash(payload), job_id=kw.get('job_id')))
    shadow = Shadow()
    for i, value in enumerate(feedback):
        call = ToolCall(call_id=value['call_id'], name='read_file', arguments={
            'path': value['data']['path'], 'offset': value['data']['offset'], 'max_bytes': 16000})
        job = 'offline-reducer-' + str(i)
        rows.append(SimpleNamespace(kind='agent_v2_tool_result', actor_id='orchestrator', job_id=job,
            payload={'artifact_id': value['receipt_artifact'], 'request_arguments': call.arguments}))
        record_tool(shadow, shadow.checkpoint(), call, value['receipt_artifact'], batch_complete=i == len(feedback) - 1, job_id=job)
    prompt['implementation_progress'] = prompt_progress(shadow)
    batch_id = shadow.artifact({'version': 'offline-batch-projection-v1', 'original_response_artifact':
        'f941ae3218fd1994b1e683e8ab0a57d4cc788a6ed4700624bd1d05f0fb8feee3',
        'binding_hash': content_hash(binding.model_dump()), 'source_hash': prompt['source_hash'],
        'call_ids': [r['call_id'] for r in feedback]})
    aggregate_id = shadow.artifact({'version': 'native-read-batch-result-v1', 'batch_artifact': batch_id,
        'response_artifact': 'f941ae3218fd1994b1e683e8ab0a57d4cc788a6ed4700624bd1d05f0fb8feee3',
        'binding_hash': content_hash(binding.model_dump()), 'source_hash': prompt['source_hash'],
        'call_ids': [r['call_id'] for r in feedback], 'receipts': [r['receipt_artifact'] for r in feedback]})
    prompt['read_batch_observation'] = {'artifact_id': aggregate_id, 'batch_artifact': batch_id, 'status': 'completed',
        'instruction': 'All completed calls are observed in original order. A read batch does not complete Coding or Verify.'}
    prompt["approved_task_input"] = {"partial": True, "artifact_id": input_id,
        "total_bytes": len(canonical_json(inputs).encode()), "instruction": "Read the approved plan and Issue using read_artifact before implementing; a goal alone is not the complete acceptance contract."}
    for value in feedback:
        d = value["data"]
        prompt["history_index"].append({"tool": "read_file", "path": d["path"], "status": value["status"],
            "artifact_id": value["receipt_artifact"], "content_hash": d["content_hash"],
            "source_hash": value["source_hash"], "output_source_hash": value["output_source_hash"],
            **{k: d[k] for k in ("offset", "next_offset", "total_bytes")}})
    def render(base):
        history = {"version": "agent-tool-history-index-v1", "session_id": "offline-projection",
            "binding_hash": content_hash(binding.model_dump()), "source_hash": base["source_hash"], "rows": base["history_index"]}
        aid = content_hash(history); artifacts[aid] = history
        return project_native_prompt(base, spec, history_artifact=aid, task_input=inputs)
    observed = render(prompt)
    assert native_input_bound(observed) <= 32000
    rows = []
    recovery = []
    for original, visible in zip(feedback, observed["feedback"], strict=True):
        d, shown = original["data"], visible["data"]
        aid = original["receipt_artifact"]
        source_aid = d.get("source_page_artifact")
        encoded = d["text"].encode() if source_aid else canonical_json(artifacts[aid]).encode()
        if source_aid:
            import hashlib
            assert hashlib.sha256(encoded).hexdigest() == source_aid
            artifacts[source_aid] = encoded
        restored = b""; offset = 0; rounds = 0; recovery_pages = []
        # Exercise exactly the existing byte-page and Native Prompt projection,
        # including a second truncation of read_artifact's JSON text if needed.
        while offset < len(encoded):
            assert rounds < 64
            page = artifact_page(encoded, offset, 16000)
            result = ToolResult(call_id="offline-artifact-" + str(rounds), input_hash="f" * 64,
                source_hash=prompt["source_hash"], output_source_hash=prompt["source_hash"], status="succeeded",
                data={"tool": "read_artifact", "artifact_id": source_aid or aid, **page}).model_dump()
            rid = content_hash(result); artifacts[rid] = result
            follow = {**prompt, "feedback": [{**result, "receipt_artifact": rid}]}
            projected = render(follow); assert native_input_bound(projected) <= 32000
            actual = projected["feedback"][0]["data"]
            chunk = actual["text"].encode(); assert chunk and chunk == encoded[offset:offset + len(chunk)]
            assert actual["next_offset"] == (offset + len(chunk) if offset + len(chunk) < len(encoded) else None)
            recovery_pages.append({'requested_offset': offset, 'requested_max_bytes': 16000,
                'tool_returned_bytes': len(page['text'].encode()), 'prompt_visible_bytes': len(chunk),
                'next_offset': actual['next_offset'], 'total_bytes': actual['total_bytes'],
                'receipt_artifact': rid, 'native_input_bound': native_input_bound(projected)})
            restored += chunk; offset += len(chunk); rounds += 1
        assert restored == encoded
        assert (restored.decode() if source_aid else json.loads(restored)["data"]["text"]) == d["text"]
        import hashlib
        full_source = CONTENTS[d['path']].encode()
        assert hashlib.sha256(full_source).hexdigest() == d['content_hash']
        reconstructed = d['text'].encode(); cursor = d['offset'] + len(reconstructed)
        continuation = []
        while cursor < len(full_source):
            page = artifact_page(full_source, cursor, 16000)
            raw_id = hashlib.sha256(page['text'].encode()).hexdigest()
            artifacts[raw_id] = page['text'].encode()
            from app.agent_v2.read_progress import ReadProgressNote, _key
            from app.agent_v2.contracts import ReadFile
            note = ReadProgressNote(range_key=_key(prompt['source_hash'], d['content_hash'], ReadFile(path=d['path'], offset=cursor, max_bytes=16000)),
                max_bytes=16000, prior_same_range_reads=0, prior_receipt=None, warning_receipts_seen=[], visible_bytes=0, visible_next_offset=None)
            result = ToolResult(call_id='offline-source-continuation-' + str(cursor), input_hash='f' * 64,
                source_hash=prompt['source_hash'], output_source_hash=prompt['source_hash'], status='succeeded',
                data={'tool': 'read_file', 'path': d['path'], 'execution_backend': 'docker',
                    'content_hash': d['content_hash'], 'source_page_artifact': raw_id, 'source_read_progress': note.model_dump(), **page}).model_dump()
            rid = content_hash(result); artifacts[rid] = result
            projected = render({**prompt, 'feedback': [{**result, 'receipt_artifact': rid}]})
            actual = projected['feedback'][0]['data']; chunk = actual['text'].encode()
            assert native_input_bound(projected) <= 32000 and chunk and chunk == full_source[cursor:cursor + len(chunk)]
            continuation.append({'requested_offset': cursor, 'requested_max_bytes': 16000,
                'tool_returned_bytes': len(page['text'].encode()), 'prompt_visible_bytes': len(chunk),
                'visible_next_offset': actual['next_offset'], 'native_input_bound': native_input_bound(projected),
                'receipt_artifact': rid, 'source_page_artifact': raw_id})
            reconstructed += chunk; cursor += len(chunk)
            assert len(continuation) <= 16
        assert d['offset'] == 0 and reconstructed == full_source
        rows.append({"path": d["path"], "tool_returned_source_bytes": len(d["text"].encode()),
            "prompt_inline_source_bytes": len(shown["text"].encode()), "offset": shown["offset"],
            "next_offset": shown["next_offset"], "total_file_bytes": shown["total_bytes"],
            "original_page_next_offset": d["next_offset"], "entire_returned_page_visible": shown["text"] == d["text"],
            "entire_file_visible": shown["text"] == d["text"] and d["offset"] == 0 and d["next_offset"] is None,
            "source_page_artifact": source_aid, "raw_source_artifact_hash_verified": bool(source_aid),
            "receipt_artifact": aid, "receipt_hash_verified": content_hash(artifacts[aid]) == aid,
            "read_artifact_recovers_entire_returned_page": True, "artifact_recovery_rounds": rounds,
            "artifact_recovery_pages": recovery_pages,
            "source_continuation_allowed": shown["next_offset"] is not None,
            "source_tail_continuation": continuation, "entire_file_recovered_offline": True,
            "reconstructed_file_hash": hashlib.sha256(reconstructed).hexdigest()})
        recovery.append({"path": d["path"], "artifact": source_aid or aid, "rounds": rounds, "bytes": len(restored)})
    return {"classification": "offline_frozen_Issue_Plan_projection_synthetic_authority_deterministic_tools",
        "original_frozen_task_input_artifact": frozen_input_id, "projection_task_input_artifact": input_id,
        "live_Plan14_binding_reused_for_execution": False, "prompt_actually_sent": False, "paid_model_calls": 0,
        "remaining_budget": remaining, "new_tool_evidence_output_bytes": output_delta,
        "ledger_reducer": "production_record_tool_over_memory_only_saved_prefix",
        "native_input_limit": 32000, "native_input_bound": native_input_bound(observed), "rows": rows,
        "returned_source_bytes": sum(r["tool_returned_source_bytes"] for r in rows),
        "inline_source_bytes": sum(r["prompt_inline_source_bytes"] for r in rows), "recovery": recovery,
        "focused_artifact_recovery_rounds_total": sum(r["rounds"] for r in recovery),
        "remaining_file_tail_read_file_calls": sum(len(r['source_tail_continuation']) for r in rows),
        "not_a_prediction_of_real_provider_Token_usage_or_model_reasoning": True}, observed, artifacts
