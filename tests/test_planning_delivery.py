"""Previewed delivery fits new pages without rewriting already issued evidence."""
import asyncio
from copy import deepcopy
import hashlib
import json

import pytest
from sqlalchemy import func, select

from app.context_compaction import ContextRead, compact_prompt, read_context
from app.database import Database
from app.models import Artifact
from app.planner import PlanningService, PlanningTurnWorker
from app.planning_delivery import MAX_DELIVERY_PASSES, fit_context_pages, limit_context_pages
from app.planning_budget import planning_usage
from app.provenance import canonical_json
from app.workbench import WorkbenchError
from tests.test_context_compaction import large_fixture, packet
from tests.test_nvidia_workflows import ScriptedRunner, _identity
from tests.test_workbench import result


def pages():
    return [{"text":"源🙂"*2000,"offset":17,"next_offset":None,"total_bytes":14017,
        "source_complete":True,"partial":False,"source_hash":"a"*64} for _ in range(4)]


def test_fair_delivery_is_a_true_utf8_prefix_with_continuation_and_no_false_completeness():
    original = pages()
    before = deepcopy(original)
    limited = limit_context_pages(original,4000)
    assert original == before
    assert sum(len(page["text"].encode()) for page in limited)<=4000
    sizes = [len(page["text"].encode()) for page in limited]
    assert max(sizes)-min(sizes)<=7
    for old,new in zip(original,limited):
        count=len(new["text"].encode())
        assert old["text"].encode().startswith(new["text"].encode())
        assert new["next_offset"]==17+count and new["partial"] and not new["source_complete"]
        assert new["total_bytes"]==old["total_bytes"] and new["source_hash"]==old["source_hash"]
        assert new["delivery"]["delivered_bytes"]==count
    assert limit_context_pages(original,100000)==original


def test_delivery_uses_actual_serialized_headroom_and_keeps_immutable_history():
    original = pages()
    fixed = {"rules":["r"*80000],"current_plan":{"exact":"frozen"},"prior_read":{"text":"ACTUAL_PRIOR_PAGE"}}
    def preview(current):
        return canonical_json({**fixed,"context_reads":current})
    delivered,receipt = fit_context_pages(original,preview)
    assert len(preview(original).encode())>96000>=len(preview(delivered).encode())
    assert receipt["limited"] and receipt["preview_passes"]<=MAX_DELIVERY_PASSES
    assert json.loads(preview(delivered))["prior_read"]==fixed["prior_read"]
    assert original[0]["source_complete"] is True
    with pytest.raises(WorkbenchError) as caught:
        fit_context_pages(original,lambda current:canonical_json({"rules":["x"*97000],"context_reads":current}))
    assert caught.value.code=="planning_context_limit"


def test_preview_creates_no_artifacts_or_issued_checkpoint_and_matches_final_hash(tmp_path):
    db,s,task,_,_=large_fixture(tmp_path)
    with db.session() as session:
        wb=PlanningService(session,s.artifact_root)
        payload=packet(wb,task)
        count=session.scalar(select(func.count()).select_from(Artifact))
        history=[(e.id,e.record_hash) for e in wb.history(task)]
        prompt,preview=compact_prompt(wb,task,payload,scope="planning",page_source_files=True,record_checkpoint=False)
        assert session.scalar(select(func.count()).select_from(Artifact))==count
        assert [(e.id,e.record_hash) for e in wb.history(task)]==history
        with pytest.raises(WorkbenchError) as caught:
            read_context(wb,task,preview["artifact_id"],[ContextRead(reference_id="a"*64)])
        assert caught.value.code=="context_reference_invalid"
        recorded,checkpoint=compact_prompt(wb,task,payload,scope="planning",page_source_files=True)
        assert prompt==recorded and checkpoint==preview
        assert len([e for e in wb.history(task) if e.kind=="context_checkpoint"])==1
    db.close()


@pytest.mark.parametrize("mixed", [False,True])
def test_worker_delivers_only_admitted_pages_and_preserves_cache_across_restart(tmp_path,monkeypatch,mixed):
    import app.planner as planner
    db,s,task,content,_=large_fixture(tmp_path)
    original_compact=planner.compact_prompt
    def with_pinned_header(wb,task,payload,**kw):
        body=deepcopy(payload);body["rules"].append("FIXED_FIXTURE_RULE "+"x"*60000)
        return original_compact(wb,task,body,**kw)
    monkeypatch.setattr(planner,"compact_prompt",with_pinned_header)
    with db.session() as session:
        wb=PlanningService(session,s.artifact_root)
        if mixed:
            context=wb.latest(task,"context_ready");evidence=wb.read(context.payload["artifact_id"])
            evidence["inventory"].append("safe.py")
            wb.append(task,"context_ready",{**context.payload,"artifact_id":wb.artifact(evidence)},key="mixed-delivery")
        wb.request(task,text="Prepare plan",parent_id=None,
            expected_hash=None,key="delivery",runtime_version="agent-runtime-v2")
    runner=ScriptedRunner();complete=runner.complete
    async def response(invocation):
        prompt=json.loads(invocation.prompt)
        ref=next(e["context_reference"] for e in prompt["evidence"]["entries"] if e["path"]=="background.py")
        runner.contents.append(json.dumps({"read_context":[{"reference_id":ref,"offset":offset,"max_bytes":16000}
            for offset in (0,16000,32000,48000)],**({"read_paths":["safe.py"]} if mixed else {})}))
        return await complete(invocation)
    runner.complete=response
    first=asyncio.run(PlanningTurnWorker(db,s,runner,_identity(s.implementation_model),worker_id="delivery-first").run_once())
    assert first.state=="succeeded",first.error_message
    if mixed:
        from app.archives import RepositoryArchiveStore
        from app.planner import PlanningContextWorker
        monkeypatch.setattr(RepositoryArchiveStore,"lookup",lambda *a,**kw:object())
        class Reader:
            async def inspect(self,**kw):
                assert kw["paths"]==("safe.py",)
                return {"inventory":evidence["inventory"],"entries":[{"path":"safe.py","content":"safe captured helper",
                    "prior_hash":hashlib.sha256(b"safe captured helper").hexdigest(),"executable":False}],"truncated":False}
        captured=asyncio.run(PlanningContextWorker(db,s,Reader(),worker_id="mixed-delivery").run_once())
        assert captured.state=="succeeded",captured.error_message
    db.close();db=Database(s.database_url)
    final_runner=ScriptedRunner(json.dumps({"questions":[{"id":"scope","prompt":"Which public behavior is required?",
        "options":["Preserve the current public interface","Confirm a changed interface"]}]}))
    final=asyncio.run(PlanningTurnWorker(db,s,final_runner,_identity(s.implementation_model),worker_id="delivery-final").run_once())
    assert final.state=="succeeded",final.error_message
    actual=json.loads(final_runner.invocations[0].prompt)
    assert len(final_runner.invocations[0].prompt.encode())<=96000 and len(actual["context_reads"])==4
    assert any(page.get("delivery",{}).get("limited") for page in actual["context_reads"])
    assert actual["context_read_history"]==[]  # Unseen drafts must not become historical reads.
    with db.session() as session:
        wb=PlanningService(session,s.artifact_root)
        receipt=wb.latest(task,"context_read_delivery")
        assert receipt.payload["original_prompt_bytes"]>96000 and receipt.payload["prompt_bytes"]<=96000
        assert len([e for e in wb.history(task) if e.kind=="context_checkpoint"])==2
        assert planning_usage(wb,task)["calls"]==2 and wb.latest(task,"execution_authorized") is None
        checkpoint=wb.latest(task,"context_checkpoint").payload["artifact_id"]
        for page in actual["context_reads"]:
            if page.get("delivery"):
                selector={**page["delivery"]["continuation"],"max_bytes":256}
                current=next(e["context_reference"] for e in actual["evidence"]["entries"] if e["path"]==page["source_path"])
                continuation=read_context(wb,task,checkpoint,[ContextRead(reference_id=current,**selector)])[0]
                assert continuation["offset"]==page["next_offset"] and continuation["source_hash"]==page["source_hash"]
        assert wb.latest_plan(task) is None
    db.close()
