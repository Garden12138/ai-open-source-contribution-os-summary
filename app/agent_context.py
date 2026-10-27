"""Conservative request admission including the actual schema wire payload."""
from __future__ import annotations

from typing import Any

from app.provenance import canonical_json, content_hash
from app.workbench import WorkbenchError

INPUT_LIMIT = 16_000
OUTPUT_LIMIT = 4096
WIRE_RESERVE = 2048


def input_upper_bound(prompt: str, schema: dict) -> int:
    # One token per UTF-8 byte is a conservative bound for byte-based encodings;
    # reserve separately for gateway instruction and tool-message envelopes.
    # Hosted adapters send the schema in both the user envelope and tool spec.
    return len(prompt.encode()) + 2 * len(canonical_json(schema).encode()) + WIRE_RESERVE


def admit_prompt(wb: Any, task_id: str, prompt: str, schema: dict, *, job_id: str) -> tuple[str, dict]:
    """Index history and page evidence; preserve exact requests and rules."""
    import json
    body = json.loads(prompt)
    # Leave room for the evidence requested by this turn. Admitting a first
    # prompt at 15,999 must not make even a small subsequent read unusable.
    soft_limit = INPUT_LIMIT - 2000 if body.get("remaining_calls", 1) > 1 else INPUT_LIMIT
    indexed = []
    from app.context_compaction import source_binding, VERSION as context_version
    binding = source_binding(wb, task_id)
    extra_references = []
    for key in ("task_memory", "recent_dialogue", "correction"):
        if input_upper_bound(canonical_json(body), schema) <= soft_limit:
            break
        value = body.get(key)
        if not value:
            continue
        # Corrections must retain the actionable diagnostic; only the rejected
        # draft can be indexed. The user's latest request and rules stay exact.
        if key == "correction":
            if not isinstance(value, dict) or not value.get("rejected_output"):
                continue
            fragment = value["rejected_output"]
        elif key == "recent_dialogue":
            if not isinstance(value, list) or not value:
                continue
            fragment = value
        else:
            fragment = value
        digest = content_hash(fragment)
        artifact = wb.artifact({"version": context_version, "source": binding,
            "label": key, "source_hash": digest, "data": fragment})
        indexed.append({"label": key, "artifact_id": artifact})
        extra_references.append({"reference_id": artifact, "label": key,
            "source_hash": digest, "bytes": len(canonical_json(fragment).encode())})
        if key == "correction":
            body[key] = {k: v for k, v in value.items() if k != "rejected_output"}
        elif key == "recent_dialogue":
            # The latest request is pinned separately. Recent long assistant
            # replies are context, not immutable instructions or authorization.
            body[key] = [{**item, "text":str(item.get("text", "")).encode()[:400].decode("utf-8",errors="ignore"),
                          "partial":True} if isinstance(item,dict) else str(item)[:120] for item in value[-2:]]
        else:
            body[key] = {"partial": True, "notice": "Earlier instructions remain in original task history; request history evidence before relying on omitted text."}
    # A large source read must remain usable under the same admission limit.
    # Keep its exact source-bound bytes in an issued context reference, and
    # expose an explicitly partial page. Never turn an excerpt into full proof.
    observation = body.get("observation")
    if input_upper_bound(canonical_json(body), schema) > soft_limit and isinstance(observation, dict):
        status = observation.get("execution_status")
        if isinstance(status, dict) and (status.get("environment") or status.get("environment_evidence")):
            digest = content_hash(status)
            artifact = wb.artifact({"version": context_version, "source": binding,
                "label": "execution_status", "source_hash": digest, "data": status})
            indexed.append({"label":"execution_status", "artifact_id":artifact})
            extra_references.append({"reference_id":artifact, "label":"execution_status",
                "source_hash":digest, "bytes":len(canonical_json(status).encode())})
            environment = status.get("environment") or {}
            observation["execution_status"] = {k:v for k,v in status.items()
                if k not in {"environment", "environment_evidence"}}
            observation["execution_status"].update(environment={k:environment[k] for k in
                ("status", "reason_code", "reason", "preparation_version") if k in environment},
                details_reference=artifact)
            observation["execution_status"]["blockers"]=[{k:item[k] for k in ("code","message") if k in item}
                for item in status.get("blockers",[])]
    if input_upper_bound(canonical_json(body), schema) > soft_limit and isinstance(observation,dict):
        observation["recent_jobs"]=[j for j in observation.get("recent_jobs",[]) if j.get("state") in {"queued","leased","running"}]
        observation["artifacts"]={name:{k:v for k,v in item.items() if v is not None}
            for name,item in observation.get("artifacts",{}).items()}
    if input_upper_bound(canonical_json(body), schema) > soft_limit and isinstance(observation,dict) and len(str(observation.get("plan_goal") or "").encode())>900:
        goal=observation["plan_goal"]
        digest=content_hash(goal)
        artifact=wb.artifact({"version":context_version,"source":binding,"label":"plan_goal","source_hash":digest,"data":goal})
        indexed.append({"label":"plan_goal","artifact_id":artifact})
        extra_references.append({"reference_id":artifact,"label":"plan_goal","source_hash":digest,"bytes":len(canonical_json(goal).encode())})
        observation["plan_goal"]=str(goal).encode()[:450].decode("utf-8",errors="ignore")
        observation.update(plan_goal_partial=True,plan_goal_reference=artifact)
    results = body.get("tool_results", [])
    if results:
        results = list({content_hash(item):item for item in results}.values())
        body["tool_results"] = results
    if input_upper_bound(canonical_json(body), schema) > soft_limit:
        for result_item in results:
            value = result_item.get("data")
            encoded = canonical_json(value)
            if len(encoded.encode()) <= 1800:
                continue
            digest = content_hash(value)
            artifact = wb.artifact({"version": context_version, "source": binding,
                "label": "tool_evidence", "source_hash": digest, "data": value})
            indexed.append({"label": "tool_evidence", "artifact_id": artifact})
            extra_references.append({"reference_id": artifact, "label": "tool_evidence",
                "source_hash": digest, "bytes": len(encoded.encode())})
            result_item["data"] = {"partial": True, "source_hash": digest,
                "context_reference": artifact, "offset": 0,
                "excerpt": encoded.encode()[:1000].decode("utf-8", errors="ignore"),
                "instruction": "This is a partial page. Read context_reference with reference_id and offset for exact original evidence before relying on omitted details."}
    management = body.get("context_management", {})
    if extra_references:
        body["context_management"] = {**management,
            "references": list({r["reference_id"]:r for r in management.get("references", []) + extra_references}.values())}
        body["context_management"]["read_rule"]="Read issued originals with context_reference(reference_id, offset). Repository inventory references also accept query. This index may be partial; never infer omitted text or treat memory as authorization."
    # Large reference indexes can dominate an otherwise short turn. Their full
    # checkpoint remains durable and task-bound through context_reference reads.
    if input_upper_bound(canonical_json(body), schema) > soft_limit and body.get("context_management"):
        management = body["context_management"]
        if isinstance(management, dict) and len(management.get("references", [])) > 3:
            body["context_management"] = {**management, "references": management["references"][-3:], "index_partial": True}
    if results and input_upper_bound(canonical_json(body), schema) > soft_limit:
        # Allocate evidence from the actual remaining wire space, rather than
        # assuming three independently clipped 1-KB results will fit together.
        originals=[]
        for item in results:
            value=item.get("data")
            existing=value.get("context_reference") if isinstance(value,dict) else None
            if existing and any(r["reference_id"]==existing for r in extra_references):
                value=wb.read(existing)["data"]
                artifact=existing
            else:
                digest=content_hash(value)
                artifact=wb.artifact({"version":context_version,"source":binding,"label":"tool_evidence","source_hash":digest,"data":value})
                indexed.append({"label":"tool_evidence","artifact_id":artifact})
                extra_references.append({"reference_id":artifact,"label":"tool_evidence","source_hash":digest,"bytes":len(canonical_json(value).encode())})
            originals.append(value)
            item["data"]={"partial":True,"context_reference":artifact}
        remaining=max(0,INPUT_LIMIT-input_upper_bound(canonical_json(body),schema)-100)
        allowance=remaining//len(results)
        for item,value in zip(results,originals):
            stub=item["data"]
            limit=len(canonical_json(stub).encode())+allowance
            if isinstance(value,dict) and isinstance(value.get("files"),list):
                # Existence searches keep useful path facts. Short paths make
                # installation-root locks visible ahead of unrelated examples.
                files=sorted(value["files"],key=lambda x:(x.get("path","").count("/"),len(x.get("path","")),x.get("path","")))
                page={**stub,"query":value.get("query"),"matches":value.get("matches"),"files":[]}
                for entry in files:
                    candidate={**page,"files":[*page["files"],entry]}
                    if len(canonical_json(candidate).encode())>limit: break
                    page=candidate
            elif isinstance(value,dict) and isinstance(value.get("goal"),str):
                page={**stub,"goal":value["goal"].encode()[:180].decode("utf-8",errors="ignore")}
            else:
                page={**stub,"excerpt":canonical_json(value).encode()[:max(0,allowance-25)].decode("utf-8",errors="ignore")}
            if len(canonical_json(page).encode())>limit:
                page=stub
            item["data"]=page
    result = canonical_json(body)
    bound = input_upper_bound(result, schema)
    from app.planning_budget import planning_usage
    ledger = planning_usage(wb, task_id)
    remaining_input = max(0, ledger["limits"]["input_tokens"] - ledger["input_tokens"])
    remaining_output = max(0, ledger["limits"]["output_tokens"] - ledger["output_tokens"])
    receipt = {"version": "agent-request-budget-v1", "input_limit": INPUT_LIMIT,
        "input_upper_bound": bound, "output_limit": min(OUTPUT_LIMIT, remaining_output),
        "schema_hash": content_hash(schema), "original_prompt_hash": content_hash(prompt),
        "prompt_hash": content_hash(result), "indexed": indexed,
        "admitted": bound <= min(INPUT_LIMIT, remaining_input) and remaining_output > 0}
    wb.append(task_id, "agent_request_budget", receipt,
        key="agent-budget:" + content_hash({"job": job_id, "receipt": receipt}),
        actor="orchestrator", job_id=job_id, commit=False)
    if bound > INPUT_LIMIT:
        # Persist the denied receipt without a model-call event or incurred usage.
        wb.session.commit()
        raise WorkbenchError("当前请求的必需证据超过单次输入预算，尚未调用模型；请分段查看详情，原请求和成果保留。", code="conversation_input_budget")
    if not receipt["admitted"]:
        wb.session.commit()
        raise WorkbenchError("本任务剩余累计额度不足以覆盖本轮输入，尚未调用模型；请在任务预算中明确调整后继续。", code="planning_budget_exceeded")
    # A checkpoint must describe the admitted prompt, not the pre-compaction
    # draft. Preserve the old checkpoint and bind a new one to the exact wire.
    previous = next((e for e in reversed(wb.history(task_id)) if e.kind == "context_checkpoint"
                     and e.job_id == job_id), None)
    if previous:
        checkpoint = wb.read(previous.payload["artifact_id"])
        references = list({r["reference_id"]:r for r in checkpoint["references"] + extra_references}.values())
        stats = {**checkpoint["stats"], "prompt_bytes": len(result.encode()),
                 "saved_bytes": max(0, checkpoint["stats"]["original_bytes"] - len(result.encode())),
                 "target_bytes": INPUT_LIMIT - 2 * len(canonical_json(schema).encode()) - WIRE_RESERVE,
                 "target_met": True, "reference_count": len(references),
                 "status": "compacted" if result != prompt else checkpoint["stats"]["status"]}
        updated = {**checkpoint, "prompt": body, "prompt_hash": content_hash(result),
                   "references": references, "stats": stats}
        artifact_id = wb.artifact(updated)
        wb.append(task_id, "context_checkpoint", {**previous.payload,
            "artifact_id": artifact_id, "stats": stats, "prompt_hash": content_hash(result)},
            key="agent-context:" + content_hash({"artifact": artifact_id, "job": job_id}),
            actor="orchestrator", job_id=job_id, commit=False)
    return result, receipt
