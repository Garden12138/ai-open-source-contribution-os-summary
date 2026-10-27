"""Representation correction preserves the model's exact bytes and authority."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json

import pytest

from app.proposal_recovery import normalize_edit_append, record_edit_append_correction
from app.proposal_validation import check_proposal_structure, ProposalValidationError
from tests.test_execution_repair import context_for


def split_draft(context):
    entry = context.entries[0]
    common = {"path":entry.path,"expected_prior_hash":entry.prior_hash,"executable":entry.executable}
    return {"summary":"Add exact regression coverage", "operations":[
        {**common,"kind":"edit","content":None,"edits":[{"old_text":"original","new_text":"updated"}]},
        {**common,"kind":"append","content":"\r\n# exact appended bytes\r\n","edits":[]},
    ]}


@pytest.mark.parametrize("executable", [False, True])
def test_only_equivalent_edit_append_representation_changes(executable):
    from dataclasses import replace
    from app.sandbox_worker.coding_context import CodingContext
    from app.coding import _parse_change_set_proposal
    context = context_for("# original\r\n", "test_case.py")
    data = context.hash_payload();data.pop("version")
    data["entries"] = (replace(context.entries[0], executable=executable),)
    context = CodingContext.create(**data)
    draft = split_draft(context);frozen = deepcopy(draft)
    with pytest.raises(ProposalValidationError):
        check_proposal_structure(json.dumps(draft), context, ["test_case.py"])
    normalized, proof = normalize_edit_append(json.dumps(draft), context, ["test_case.py"])
    assert draft == frozen
    assert proof["byte_equivalence_verified"] and proof["model_calls_added"] == 0
    assert proof["raw_operation_count"] == 2 and proof["normalized_operation_count"] == 1
    _, operations = _parse_change_set_proposal(normalized, context, strict_prior_hash=True)
    assert operations[0].content == "# updated\r\n\r\n# exact appended bytes\r\n"
    assert operations[0].executable is executable
    assert operations[0].expected_prior_hash == context.entries[0].prior_hash


@pytest.mark.parametrize("invalid", ["two_edits","two_appends","append_then_edit","wrong_hash","outside",
    "missing_anchor","mode_change","numeric_mode","extra_fields","append_edits","secret","third_operation","no_pair"])
def test_format_correction_never_guesses_or_loosens_input_guards(invalid):
    context = context_for("original\n", "test_case.py")
    draft = split_draft(context)
    a,b = draft["operations"]
    if invalid == "two_edits": b.update(kind="edit", content=None, edits=a["edits"])
    if invalid == "two_appends": a.update(kind="append", content="extra", edits=[])
    if invalid == "append_then_edit": draft["operations"].reverse()
    if invalid == "wrong_hash": b["expected_prior_hash"] = "f"*64
    if invalid == "outside": b["path"] = "outside.py"
    if invalid == "missing_anchor": a["edits"][0]["old_text"] = "invented source"
    if invalid == "mode_change": b["executable"] = True
    if invalid == "numeric_mode": b["executable"] = 0
    if invalid == "extra_fields": b["extra"] = "not allowed"
    if invalid == "append_edits": b["edits"] = a["edits"]
    if invalid == "secret": b["content"] = "ghp_FORMATCORRECTIONCANARY1234567890"
    if invalid == "third_operation": draft["operations"].append(dict(b))
    if invalid == "no_pair": draft["operations"] = [a]
    with pytest.raises(ValueError):
        normalize_edit_append(json.dumps(draft), context, ["test_case.py"])


def direct_rejected(tmp_path, monkeypatch):
    from app.contribution_workflow import advance_workflows
    from app.workbench import Workbench
    from tests.test_coding_source_reads import direct_fixture
    from tests import test_workbench as fixtures
    db,s,task,workers,code,review = direct_fixture(tmp_path,monkeypatch)
    fixtures.populate_round(code,review)
    for _ in range(8):
        for i,worker in enumerate(workers):
            if i != 2:
                job = asyncio.run(worker.run_once())
                if job: assert job.state == "succeeded", job.error_message
        advance_workflows(db,s)
        with db.session() as session:
            w=Workbench(session,s.artifact_root)
            root=next((j for j in w.jobs(task) if j.kind=="change_set_proposal" and j.state=="queued"),None)
            if root: break
    else: raise AssertionError("No direct proposal")
    text="original content for app/plans.py\n"
    common={"path":"app/plans.py","expected_prior_hash":hashlib.sha256(text.encode()).hexdigest(),"executable":False}
    raw=json.dumps({"summary":"Preserve exact requested changes", "operations":[
        {**common,"kind":"edit","content":None,"edits":[{"old_text":text,"new_text":"def preserved_api():\n    return True\n"}]},
        {**common,"kind":"append","content":"\n# regression coverage\n","edits":[]}]})
    code.contents[:]=[raw]*3
    for _ in range(3): asyncio.run(workers[2].run_once())
    advance_workflows(db,s)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        assert w.active_blocker(task)
        # Three historical read calls, plus the three rejected invocations.
        for i in range(3):w.append(task,"agent_code_call",{"root_job_id":root.id},key=f"past-read:{i}",actor="orchestrator")
        rejection=w.latest(task,"proposal_rejected")
    return db,s,task,workers,code,root.id,rejection.record_hash


def test_direct_correction_at_call_limit_reaches_verify_without_new_model_calls(tmp_path,monkeypatch):
    from app.workbench import Workbench
    from app.models import Job
    from app.coding_budget import proposal_budget
    from tests.test_workbench import drive_round
    db,s,task,workers,code,jid,rejection_hash=direct_rejected(tmp_path,monkeypatch)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        original={e.id:e.record_hash for e in w.history(task)}
        auth=w.latest(task,"execution_authorized").record_hash
        receipt=record_edit_append_correction(w,jid,expected_rejection_hash=rejection_hash,settings=s)
        assert receipt.payload["model_calls_added"]==0
        assert record_edit_append_correction(w,jid,expected_rejection_hash=rejection_hash,settings=s).id==receipt.id
        assert proposal_budget(w,task,jid)["used"]==6
        assert session.get(Job,jid).state=="failed"
        assert session.get(Job,jid).attempt_count==session.get(Job,jid).max_attempts==3
        event=w.latest(task,"proposal_format_normalized")
        proof=w.read(event.payload["proof_hash"])
        assert proof["byte_equivalence_verified"] and proof["rejection_hash"]==rejection_hash
        assert w.latest(task,"agent_maintenance_recorded")
        assert all({e.id:e.record_hash for e in w.history(task)}.get(i)==h for i,h in original.items())
    drive_round(db,s,task,workers)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        assert w.latest(task,"awaiting_acceptance")
        assert w.latest(task,"execution_authorized").record_hash==auth
        assert proposal_budget(w,task,jid)["used"]==6
        assert len(code.invocations)==3
        assert not w.latest(task,"publication_confirmed")
        assert record_edit_append_correction(w,jid,expected_rejection_hash=rejection_hash,settings=s).id==receipt.id
    db.close()


@pytest.mark.parametrize("invalid", ["stop","rejection","conversation","busy","untrusted_rejection"])
def test_direct_correction_requires_current_exact_model_evidence(tmp_path,monkeypatch,invalid):
    from app.workbench import Workbench, WorkbenchError
    from app.coding import CodingConversationService
    from app.models import Job
    db,s,task,workers,code,jid,rejection_hash=direct_rejected(tmp_path,monkeypatch)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        if invalid=="stop":w.append(task,"stop_requested",{},key="stop-format")
        if invalid=="rejection":rejection_hash="a"*64
        if invalid=="conversation":
            CodingConversationService(session,artifact_root=s.artifact_root).send_message(session.get(Job,jid).payload["execution_attempt_id"],content="Changed input",idempotency_key="changed-input")
        if invalid=="busy":w.enqueue(task,"execution_question",{},key="busy-format")
        if invalid=="untrusted_rejection":
            old=w.latest(task,"proposal_rejected")
            event=w.append(task,"proposal_rejected",old.payload,key="untrusted-format",actor="model",job_id=jid,plan_id=old.plan_version_id)
            rejection_hash=event.record_hash
        with pytest.raises((ValueError, WorkbenchError)):
            record_edit_append_correction(w,jid,expected_rejection_hash=rejection_hash,settings=s)
        session.rollback()
        assert not w.latest(task,"proposal_corrected")
        assert not w.latest(task,"proposal_format_normalized")
    db.close()


def test_normalization_and_correction_roll_back_together(tmp_path,monkeypatch):
    from app.workbench import Workbench
    import app.proposal_recovery as recovery
    db,s,task,workers,code,jid,rejection_hash=direct_rejected(tmp_path,monkeypatch)
    def fail(*args,**kwargs):raise ValueError("Injected correction failure")
    monkeypatch.setattr(recovery,"record_corrected_proposal",fail)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        with pytest.raises(ValueError):record_edit_append_correction(w,jid,expected_rejection_hash=rejection_hash,settings=s)
        assert not w.latest(task,"proposal_format_normalized")
        assert not w.latest(task,"proposal_corrected")
    db.close()
