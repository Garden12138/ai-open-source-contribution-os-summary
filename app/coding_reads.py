"""Bounded supplemental source reads; reading never expands approved edit scope."""
from __future__ import annotations

from typing import Any

from app.provenance import content_hash
from app.workbench import Workbench, WorkbenchError

KIND = "coding_source_read"
VERSION = "coding-source-read-v1"


def read_cache(wb: Workbench, task_id: str, root_job_id: str, view: Any) -> dict:
    """Retain bounded supplemental evidence across reads of one editing view."""
    from app.models import Job
    from app.sandbox_worker.coding_context import CodingContextEntry
    captured = {entry.path for entry in view.entries if entry.content is not None}
    entries, references = {}, []
    used = 0
    for event in reversed(wb.history(task_id)):
        if (event.kind != "agent_code_tool_result" or event.actor_id != "orchestrator"
                or event.payload.get("context_hash") != view.context_hash):
            continue
        job = wb.session.get(Job, event.job_id)
        if (job is None or job.state != "succeeded" or job.payload_hash != content_hash(job.payload)
                or job.payload.get("root_job_id", job.id) != root_job_id
                or job.payload.get("execution_attempt_id") != view.execution_attempt_id):
            continue
        value = wb.read(event.payload["artifact_id"])
        if value.get("context_hash") != view.context_hash:
            raise WorkbenchError("源码缓存的编辑依据不匹配。", code="coding_read_stale")
        for raw in value.get("entries", []):
            if len(references) >= 32:
                break
            entry = CodingContextEntry(**raw)
            if entry.path in captured or entry.path in entries or entry.content is None:
                continue
            size = len(entry.content.encode())
            included = used + size <= 64_000
            references.append({"path":entry.path,"content_hash":entry.prior_hash,
                "artifact_id":event.payload["artifact_id"],"included":included})
            if included:
                entries[entry.path] = entry.to_wire()
                used += size
    return {"version":"coding-read-cache-v1","context_hash":view.context_hash,
        "entries":list(entries.values()),"references":references[:32],"read_only":True,
        "instruction":"These frozen helper files were already read. Reuse their complete content; reading never expands approved edit paths."}


def recovered_source_read(wb: Workbench, attempt_id: str) -> Any | None:
    """Resume a retained read under its original, bounded proposal lineage."""
    import json
    from app.models import Job
    from app.executions import ExecutionAttemptService
    from app.coding import _load_coding_context
    from app.coding_repair import repair_change_set,repair_context
    attempt=ExecutionAttemptService(wb.session).get_verified(attempt_id)
    history=wb.history(attempt.task_id)
    repair=next((e for e in reversed(history) if e.kind=="preparation_runtime_retry_requested"
        and e.actor_id=="orchestrator" and e.payload.get("attempt_id")==attempt_id and e.payload.get("rejected_read_hash")),None)
    if repair is None:return None
    rejection=next((e for e in history if e.record_hash==repair.payload["rejected_read_hash"] and e.kind=="proposal_rejected"),None)
    failed=wb.session.get(Job,rejection.job_id) if rejection else None
    if failed is None or failed.state!="failed" or failed.error_code!="proposal_structure_invalid":
        raise WorkbenchError("原补读请求已变化。",code="coding_read_stale")
    key="recover-source-read:"+failed.id
    existing=next((j for j in wb.jobs(attempt.task_id) if j.idempotency_key==key),None)
    if existing:return existing
    coding,attempt,context=_current(wb,failed.payload)
    raw=json.loads(wb.read(rejection.payload["result_hash"])["raw_proposal"])
    paths=raw.get("read_paths")
    if (raw.get("operations")!=[] or not isinstance(paths,list) or not 1<=len(paths)<=4
            or any(not isinstance(p,str) for p in paths)):
        return None
    from app.plans import _repository_path
    for path in paths:_repository_path(path,"Coding read path")
    base=_load_coding_context(wb.session,artifact_root=str(wb.artifacts.root),coding_session=coding)
    plan=wb.latest_plan(attempt.task_id)
    view=repair_context(base,repair_change_set(wb.session,str(wb.artifacts.root),base),allowed_paths=plan.files_likely_to_change)
    request={"version":VERSION,"context_hash":view.context_hash,"paths":paths,"repository_archive_hash":attempt.repository_archive_hash}
    auth=wb.latest(attempt.task_id,"execution_authorized")
    return wb.enqueue(attempt.task_id,KIND,{**failed.payload,"root_job_id":failed.payload.get("root_job_id",failed.id),
        "source_job_id":failed.id,"read_paths":paths,"authorization_hash":auth.record_hash,
        "editing_context_hash":view.context_hash,"planning_context_hash":context.record_hash,
        "request_artifact_id":wb.artifact(request),"agent_invocation_id":rejection.payload["agent_invocation_id"]},
        key=key,commit=True)


def _current(wb: Workbench, payload: dict) -> tuple[Any, Any, Any]:
    from app.coding import CodingConversationService, _require_agent_job_binding, coding_conversation_hash
    from app.executions import ExecutionAttemptService
    conversation = CodingConversationService(wb.session, artifact_root=str(wb.artifacts.root))
    coding = conversation.require_session(payload["execution_attempt_id"])
    conversation._require_session_current(coding)
    _require_agent_job_binding(payload, coding)
    if coding_conversation_hash(conversation.turns(coding.id)) != payload["conversation_hash"]:
        raise WorkbenchError("代码阅读的讨论依据已变化。", code="coding_read_stale")
    attempt = ExecutionAttemptService(wb.session).get_verified(coding.execution_attempt_id)
    from app.execution_outcome import require_coding_environment
    require_coding_environment(wb, attempt.id)
    auth = wb.latest(attempt.task_id, "execution_authorized")
    stop = wb.latest(attempt.task_id, "stop_requested")
    plan = wb.latest_plan(attempt.task_id)
    if (auth is None or auth.plan_version_id != coding.plan_version_id
            or plan is None or plan.id != coding.plan_version_id
            or (stop and stop.sequence > auth.sequence)):
        raise WorkbenchError("代码阅读的执行已停止或方案已变化。", code="coding_read_stale")
    context = wb.latest(attempt.task_id, "context_ready")
    if (context is None or context.payload["archive_hash"] != attempt.repository_archive_hash
            or context.payload["base_sha"] != attempt.base_commit_sha
            or auth.payload["context_hash"] != context.record_hash
            or (payload.get("authorization_hash") and payload["authorization_hash"] != auth.record_hash)):
        raise WorkbenchError("代码阅读的冻结源码已变化。", code="coding_read_stale")
    return coding, attempt, context


def enqueue_source_read(worker: Any, job_id: str, payload: dict, coding: Any, view: Any,
                        paths: list[str], invocation: Any, completion: Any, *,
                        attempt_number: int, now: Any) -> Any:
    from app.coding import _new_agent_invocation
    from app.jobs import JobService
    from app.plans import _repository_path
    for path in paths:
        _repository_path(path, "Coding read path")
    with worker.database.session() as session:
        w = Workbench(session, worker.artifact_root)
        worker.check(session, JobService(session).get(job_id))
        current, attempt, context = _current(w, payload)
        if current.id != coding.id:
            raise WorkbenchError("代码阅读会话已变化。", code="coding_read_stale")
        request = {"version":VERSION, "context_hash":view.context_hash, "paths":paths,
            "repository_archive_hash":attempt.repository_archive_hash}
        artifact = w.artifact(request)
        record = _new_agent_invocation(job_id=job_id, attempt_number=attempt_number, role="change_set",
            identity=worker.identity, input_hash=invocation.input_hash, output_hash=artifact,
            completion=completion, now=now)
        session.add(record)
        session.flush()
        auth = w.latest(attempt.task_id, "execution_authorized")
        child = w.enqueue(attempt.task_id, KIND, {**payload, "read_paths":paths,
            "root_job_id":payload.get("root_job_id", job_id), "source_job_id":job_id,
            "authorization_hash":auth.record_hash, "editing_context_hash":view.context_hash,
            "planning_context_hash":context.record_hash, "request_artifact_id":artifact,
            "agent_invocation_id":record.id}, key=f"coding-source-read:{job_id}:{attempt_number}", commit=False)
        w.append(attempt.task_id, "agent_code_read_requested", {**request,
            "artifact_id":artifact,"agent_invocation_id":record.id},
            key=f"coding-read-request:{job_id}:{attempt_number}", job_id=child.id,
            actor="orchestrator", commit=False)
        JobService(session).succeed(job_id, worker_id=worker.worker_id,
            result_data={"next_job_id":child.id,"source_read_requested":True}, commit=False)
        session.commit()
        return JobService(session).get(job_id)


async def complete_source_read(worker: Any, job: Any) -> dict:
    from app.archives import RepositoryArchiveStore
    from app.coding import _load_coding_context, CHANGE_SET_PROPOSAL_JOB_KIND
    from app.coding_repair import repair_change_set, repair_context
    from app.context_limits import MAX_CODING_FILE_BYTES
    from app.sandbox_worker.coding_context import CodingContextEntry
    from app.sandbox_worker.specs import SandboxPolicy
    payload = job.payload
    with worker.database.session() as session:
        worker.check(session, job)
        w = Workbench(session, worker.settings.artifact_root)
        coding, attempt, context = _current(w, payload)
        if context.record_hash != payload["planning_context_hash"]:
            raise WorkbenchError("代码阅读依据已变化。", code="coding_read_stale")
        evidence = w.read(context.payload["artifact_id"])
        inventory = set(evidence["inventory"])
        paths = payload["read_paths"]
        base_context = _load_coding_context(session, artifact_root=worker.settings.artifact_root, coding_session=coding)
        plan = w.latest_plan(attempt.task_id)
        view = repair_context(base_context, repair_change_set(session, worker.settings.artifact_root, base_context),
            allowed_paths=plan.files_likely_to_change)
        if view.context_hash != payload["editing_context_hash"]:
            raise WorkbenchError("代码编辑依据已变化。", code="coding_read_stale")
        captured = {e.path:e.to_wire() for e in view.entries if e.content is not None}
        known = [p for p in paths if p in inventory and p not in captured]
        archive_hash, repository, base, image = (attempt.repository_archive_hash,
            attempt.repository_full_name, attempt.base_commit_sha, attempt.runner_image_digest)
    entries = []
    if known:
        archive = RepositoryArchiveStore(worker.settings.artifact_root).lookup(archive_hash,
            repository_full_name=repository, base_commit_sha=base)
        if archive is None:
            raise WorkbenchError("冻结源码归档不可用。", code="coding_read_stale")
        result = await worker.runtime.inspect(archive=archive, image_digest=image,
            policy=SandboxPolicy(), query="", paths=tuple(known))
        entries = result["entries"]
    with worker.database.session() as session:
        worker.check(session, job)
        w = Workbench(session, worker.settings.artifact_root)
        coding, attempt, _ = _current(w, payload)
        base_context = _load_coding_context(session, artifact_root=worker.settings.artifact_root, coding_session=coding)
        plan = w.latest_plan(attempt.task_id)
        view = repair_context(base_context, repair_change_set(session, worker.settings.artifact_root, base_context),
            allowed_paths=plan.files_likely_to_change)
        if view.context_hash != payload["editing_context_hash"]:
            raise WorkbenchError("代码编辑依据已变化。", code="coding_read_stale")
        safe = [captured[p] for p in paths if p in captured]
        for entry in entries:
            if entry["path"] not in known or any(e["path"] == entry["path"] for e in safe):
                raise WorkbenchError("代码阅读返回了未请求的文件。", code="coding_read_invalid")
            if entry.get("content") is not None and len(entry["content"].encode()) > MAX_CODING_FILE_BYTES:
                entry = {"path":entry["path"],"content":None,"prior_hash":None,"executable":None}
            safe.append(CodingContextEntry(**entry).to_wire())
        missing = [p for p in paths if p not in inventory and p not in captured]
        parents = {p.rpartition("/")[0] for p in paths}
        candidates = sorted(p for p in inventory if p.rpartition("/")[0] in parents)[:80]
        value = {"version":VERSION,"context_hash":view.context_hash,
            "repository_archive_hash":archive_hash,"entries":safe,"missing_paths":missing,
            "unavailable_paths":[p for p in known if not any(e["path"]==p and e["content"] is not None for e in safe)],
            "directory_inventory":candidates,"read_only":True,
            "allowed_change_paths":list(plan.files_likely_to_change)}
        artifact = w.artifact(value)
        receipt = w.append(attempt.task_id, "agent_code_tool_result", {"version":"agent-runtime-v1",
            "context_hash":view.context_hash,"artifact_id":artifact,"paths":paths,
            "agent_invocation_id":payload["agent_invocation_id"]}, key="coding-source-result:"+job.id,
            actor="orchestrator", job_id=job.id, commit=False)
        child_payload = {k:v for k,v in payload.items() if k not in {"task_id","read_paths","source_job_id",
            "authorization_hash","editing_context_hash","planning_context_hash","request_artifact_id","agent_invocation_id"}}
        child = w.enqueue(attempt.task_id, CHANGE_SET_PROPOSAL_JOB_KIND, {**child_payload,
            "read_artifact_id":artifact,"read_receipt_hash":receipt.record_hash},
            key="agent-code-next:"+receipt.id, max_attempts=3, timeout_seconds=600, commit=False)
        return worker.finish(session, job, {"next_job_id":child.id,"read_artifact_id":artifact})
