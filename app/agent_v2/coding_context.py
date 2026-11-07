"""Recoverable Coding observations derived from the existing immutable Journal.

No new state store, model summary, quota or authority. A saved response confirms
request delivery; a tool receipt alone only proves availability to the platform.
"""
from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
from typing import Any

from app.provenance import canonical_json, content_hash

POLICY = {"version": "coding-loop-recovery-v1.1", "correction_after_unpatched_rounds": 6,
    "stop_after_unpatched_rounds": 8, "delivery": "committed_response_and_exact_source_bytes",
    "projection": "exact_task_brief_and_source_working_set", "working_set_max_files": 4,
    "source_page_limits": [4096, 2048, 1024, 768, 512, 256, 128],
    "handoff": "one_per_authorization_only_new_round_eight_source_actually_in_prompt",
    "selection": "related_module_least_served_round_robin_unseen_ranges",
    "module": "directory_after_common_related_parent"}
HANDOFF_VERSION = "coding-evidence-handoff-v1.1"


def merge(ranges: list[list[int]]) -> list[list[int]]:
    result: list[list[int]] = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1][1] = max(end, result[-1][1])
        else:
            result.append([start, end])
    return result


def size(ranges: list[list[int]]) -> int:
    return sum(end - start for start, end in merge(ranges))


def unseen(ranges: list[list[int]], covered: list[list[int]]) -> list[list[int]]:
    result = []
    for start, end in merge(ranges):
        for lo, hi in merge(covered):
            if hi <= start or lo >= end:
                continue
            if lo > start:
                result.append([start, lo])
            start = max(start, hi)
            if start >= end:
                break
        if start < end:
            result.append([start, end])
    return result


def handoff_used(journal: Any) -> bool:
    """The pre-dispatch prompt is the durable consumption record, even on loss.

    Scan the task's existing sessions so a repair session under the same exact
    authorization cannot obtain another exception. No new budget/state store.
    """
    auth = journal.spec().binding.authorization_hash
    events = journal.wb.history(journal.task_id) if hasattr(journal.wb, "history") else journal.events()
    for event in events:
        if event.kind != "agent_v2_model_started":
            continue
        prompt = journal.wb.read(event.payload["prompt_artifact"])
        witness = prompt.get("coding_handoff")
        if witness is not None and prompt.get("binding", {}).get("authorization_hash") == auth:
            if (not isinstance(witness, dict) or event.actor_id != "orchestrator" or witness.get("version") != HANDOFF_VERSION
                    or witness.get("authorization_hash") != auth):
                raise ValueError("coding_handoff_record_invalid")
            return True
    return False


def source_page(receipt: dict, aid: str) -> dict | None:
    """Only typed, source-bound byte pages qualify; no inferred file contents."""
    data = receipt.get("data", {})
    text, offset, total = data.get("text"), data.get("offset"), data.get("total_bytes")
    if (receipt.get("status") != "succeeded" or data.get("tool") != "read_file"
            or not isinstance(text, str) or type(offset) is not int or type(total) is not int
            or not isinstance(data.get("path"), str) or not isinstance(data.get("content_hash"), str)
            or len(data["content_hash"]) != 64 or any(c not in "0123456789abcdef" for c in data["content_hash"])
            or data.get("execution_backend") not in {"docker", "platform_receipt_reuse"} or not 0 <= offset <= total
            or offset + len(text.encode()) > total
            or data.get("next_offset") != (offset + len(text.encode()) if offset + len(text.encode()) < total else None)
            or data.get("source_page_artifact") != sha256(text.encode()).hexdigest()):
        return None
    note = data.get("source_read_progress")
    return {"receipt_artifact": aid, "source_hash": receipt["output_source_hash"],
        "read_progress_note": {k: v for k, v in note.items() if k != "instruction"} if isinstance(note, dict) else None,
        **{k: data[k] for k in ("path", "content_hash", "offset", "text", "total_bytes", "next_offset", "source_page_artifact")}}


def key(page: dict) -> tuple[str, str, str]:
    return page["source_hash"], page["path"], page["content_hash"]


def observe(journal: Any) -> tuple[dict, list[dict]]:
    """Recompute after restart, including only response-confirmed prompt bytes.

    Unknown deliveries are deliberately excluded. Historical source versions
    remain in their original artifacts and cannot verify the current workspace.
    """
    events, cp = journal.events(), journal.checkpoint()
    receipts: dict[str, dict] = {}
    pages: dict[str, dict] = {}
    available: dict[tuple, list] = defaultdict(list)
    delivered: dict[tuple, list] = defaultdict(list)
    repeated_reads = repeated_plan = 0
    plan_ranges: dict[str, list] = defaultdict(list)
    task_refs = {journal.wb.read(e.payload["prompt_artifact"])["task_context_artifact"]
        for e in events if e.kind == "agent_v2_model_started"}
    last_patch = last_patch_attempt = -1
    patch_sequences = []
    novelty = {}
    for event in events:
        if event.kind != "agent_v2_tool_result":
            continue
        aid = event.payload["artifact_id"]
        receipt = journal.wb.read(aid)
        if content_hash(receipt) != aid or event.actor_id != "orchestrator":
            raise ValueError("coding_receipt_reference_invalid")
        receipts[aid] = receipt
        data = receipt.get("data", {})
        if data.get("tool") == "apply_patch":
            last_patch_attempt = event.sequence
            if (receipt["status"] == "succeeded" and not receipt.get("failure")
                    and data.get("execution_backend") == "docker"
                    and receipt["source_hash"] != receipt["output_source_hash"]):
                last_patch = event.sequence
                patch_sequences.append(event.sequence)
        page = source_page(receipt, aid)
        if page is not None:
            # wb.read verifies the JSON receipt; verify the raw page as well.
            if sha256(journal.wb.artifacts.read_bytes(page["source_page_artifact"])).hexdigest() != page["source_page_artifact"]:
                raise ValueError("coding_source_page_invalid")
            pages[aid] = page
            if page["source_hash"] == cp.source_hash:
                ranges = available[key(page)]
                interval = [page["offset"], page["offset"] + len(page["text"].encode())]
                novelty[aid] = unseen([interval], ranges)
                repeated_reads += size(novelty[aid]) < len(page["text"].encode())
                ranges.append(interval)
        if data.get("tool") == "read_artifact" and data.get("artifact_id") in task_refs and isinstance(data.get("text"), str):
            ranges = plan_ranges[data["artifact_id"]]
            before = size(ranges)
            ranges.append([data["offset"], data["offset"] + len(data["text"].encode())])
            repeated_plan += size(ranges) - before < len(data["text"].encode())
    from app.agent_v2.model_receipts import confirmed_prompts
    starts = [e for e in events if e.kind == "agent_v2_model_started"]
    confirmed = confirmed_prompts(journal)
    no_context = 0
    for event, prompt in confirmed:
        before = sum(size(r) for r in delivered.values())
        candidates = list(prompt.get("source_working_set", []))
        for feedback in prompt.get("feedback", []):
            aid = feedback.get("receipt_artifact")
            if aid in pages:
                candidates.append({**feedback.get("data", {}), "receipt_artifact": aid})
            # Raw source pages recovered with read_artifact are equally valid.
            original = receipts.get(aid, {}).get("data", {})
            shown = feedback.get("data", {})
            if original.get("tool") == "read_artifact" and isinstance(shown.get("text"), str):
                target = original["artifact_id"]
                body = journal.wb.artifacts.read_bytes(target)
                lo, chunk = shown["offset"], shown["text"].encode()
                if body[lo:lo + len(chunk)] != chunk:
                    raise ValueError("coding_artifact_delivery_invalid")
                for page in pages.values():
                    if page["source_page_artifact"] == target:
                        candidates.append({**page, "offset": page["offset"] + shown["offset"], "text": shown["text"]})
                # A receipt JSON page may contain escaped source. Count only
                # fully delivered encoded characters, never the entire receipt.
                page = pages.get(target)
                if page is not None and page["source_hash"] == cp.source_hash:
                    marker = b'"text":' + canonical_json(page["text"]).encode()
                    if body.count(marker) != 1:
                        raise ValueError("coding_receipt_text_mapping_invalid")
                    cursor = body.index(marker) + len(b'"text":"')
                    source_offset = page["offset"]
                    for char in page["text"]:
                        width = len(canonical_json(char).encode()[1:-1])
                        end = source_offset + len(char.encode())
                        if lo <= cursor and cursor + width <= lo + len(chunk):
                            delivered[key(page)].append([source_offset, end])
                        cursor += width
                        source_offset = end
        for shown in candidates:
            page = pages.get(shown.get("receipt_artifact"))
            if page is None or page["source_hash"] != cp.source_hash or not isinstance(shown.get("text"), str):
                continue
            offset, text = shown.get("offset"), shown["text"].encode()
            if type(offset) is not int:
                continue
            local = offset - page["offset"]
            if local < 0 or local + len(text) > len(page["text"].encode()) or page["text"].encode()[local:local + len(text)] != text:
                raise ValueError("coding_delivered_source_invalid")
            delivered[key(page)].append([offset, offset + len(text)])
        if event.sequence > last_patch:
            no_context = no_context + 1 if sum(size(r) for r in delivered.values()) == before else 0
    rounds = sum(e.sequence > last_patch for e in starts)
    since_attempt = sum(e.sequence > last_patch_attempt for e in starts)
    correction = rounds >= POLICY["correction_after_unpatched_rounds"]
    used = handoff_used(journal)
    related = set(journal.spec().write_paths)
    if rounds == 8 and confirmed:
        task = journal.wb.read(confirmed[-1][1]["task_context_artifact"])
        binding = journal.spec().binding
        if (task.get("binding_hash") == content_hash(binding.model_dump())
                and content_hash(task.get("approved_plan")) == binding.plan_content_hash):
            related.update(task["approved_plan"].get("files_to_inspect", []))
    candidates = []
    current_starts = [e for e in starts if e.sequence > last_patch]
    if rounds == 8 and not used and {e.job_id for e in current_starts} <= {e.job_id for e, _ in confirmed}:
        origin = current_starts[-1]
        response = next(e for e in events if e.kind == "agent_v2_model_response" and e.job_id == origin.job_id)
        accepted = any(e.kind in {"agent_v2_turn", "agent_v2_read_batch"} and e.job_id == origin.job_id for e in events)
        if accepted:
            for e in events:
                if (e.kind != "agent_v2_tool_result" or e.sequence <= origin.sequence
                        or e.payload.get("response_artifact") != response.payload["artifact_id"]):
                    continue
                aid = e.payload["artifact_id"]
                page = pages.get(aid)
                if (page is None or page["source_hash"] != cp.source_hash or page["path"] not in related
                        or receipts[aid]["data"].get("execution_backend") != "docker" or receipts[aid].get("failure")):
                    continue
                pending = unseen(novelty.get(aid, []), delivered[key(page)])
                if pending:
                    candidates.append({"receipt_artifact": aid, "path": page["path"], "content_hash": page["content_hash"],
                        "source_hash": cp.source_hash, "ranges": pending, "origin_job_id": origin.job_id})
    candidates.sort(key=lambda p: (p["path"] not in journal.spec().write_paths, p["path"], p["ranges"]))
    candidate = candidates[0] if candidates else None
    ledger = next((e.payload["progress"] for e in reversed(events) if e.kind == "agent_v2_progress"), None)
    metrics = {"version": POLICY["version"], "source_hash": cp.source_hash,
        "model_rounds": len(starts), "rounds_without_applied_patch": rounds,
        "rounds_without_patch_attempt": since_attempt, "rounds_without_new_delivered_source": no_context,
        "first_patch_round": next((i for i, e in enumerate(starts, 1) if patch_sequences and
            e.sequence < patch_sequences[0] and (i == len(starts) or starts[i].sequence > patch_sequences[0])), None),
        "available_source_bytes": sum(size(r) for r in available.values()),
        "delivered_source_bytes": sum(size(r) for r in delivered.values()),
        "overlapping_source_reads": repeated_reads, "repeated_task_artifact_reads": repeated_plan,
        "correction_issued": correction, "stop_required": rounds > 8 or (rounds == 8 and candidate is None),
        "handoff_used": used, "handoff_candidate": candidate,
        "next_action": (ledger["next_action"] if ledger and ledger["phase"] != "investigating" else
            "use_source_evidence_for_exact_patch_or_report_specific_missing_evidence" if correction else "collect_missing_source_or_implement"),
        "absence_evidence": [{"path": r["data"]["path"], "receipt_artifact": aid}
            for aid, r in receipts.items() if r["status"] == "not_found" and r["output_source_hash"] == cp.source_hash],
        "source_index": [{"path": k[1], "content_hash": k[2], "available": merge(ranges),
            "delivered": merge(delivered[k])} for k, ranges in sorted(available.items())]}
    # One page per actual receipt/range; recent pages and approved paths first.
    unique = {}
    for page in reversed(list(pages.values())):
        if page["source_hash"] == cp.source_hash:
            interval = [page["offset"], page["offset"] + len(page["text"].encode())]
            unique.setdefault((*key(page), page["offset"], len(page["text"].encode())), {**page,
                "pending_ranges": unseen([interval], delivered[key(page)]),
                "delivered_ranges": merge(delivered[key(page)])})
    working = sorted(unique.values(), key=lambda p: p["path"] not in journal.spec().write_paths)
    return metrics, working


def select_working_set(pages: list[dict], write_paths: list[str], inspection: list[str], journal: Any | None = None,
                       required: dict | None = None) -> list[dict]:
    """Path-derived module fairness; only observed bytes, never a repository read."""
    from posixpath import commonpath, dirname
    relevant = set(write_paths) | set(inspection)
    parents = [dirname(p["path"]) for p in pages if p["path"] in relevant] or [dirname(p["path"]) for p in pages]
    base = commonpath(parents) if parents else ""
    def module(path: str) -> str:
        tail = path[len(base) + 1:] if base and path.startswith(base + "/") else path
        return (base + "/" if base else "") + (tail.split("/", 1)[0] if "/" in tail else ".")
    services: dict[str, int] = defaultdict(int)
    if journal is not None:
        from app.agent_v2.model_receipts import confirmed_prompts
        known = {key(p) for p in pages}
        for _, prompt in confirmed_prompts(journal):
            for row in prompt.get("source_working_set", []):
                aid = row.get("receipt_artifact")
                if aid:
                    prior = source_page(journal.wb.read(aid), aid)
                    if prior is not None and key(prior) in known:
                        services[module(prior["path"])] += 1
    best = {}
    for i, page in enumerate(pages):
        tier = 0 if page["path"] in write_paths else 1 if page["path"] in inspection else 2
        rank = (tier, not bool(page.get("pending_ranges")), i, page["path"])
        value = {**page, "module": module(page["path"]), "priority": tier}
        if page["path"] not in best or rank < best[page["path"]][0]:
            best[page["path"]] = (rank, value)
    selected = []
    if required:
        page = next((p for p in pages if p["receipt_artifact"] == required["receipt_artifact"]), None)
        if page is not None:
            selected.append({**page, "module": module(page["path"]), "priority": 0 if page["path"] in write_paths else 1,
                "pending_ranges": required["ranges"]})
            best.pop(page["path"], None)
    while best and len(selected) < POLICY["working_set_max_files"]:
        pool = [(rank, p) for rank, p in best.values() if p["priority"] < 2] or list(best.values())
        modules = {p["module"] for _, p in pool}
        choice = min(modules, key=lambda m: (sum(p["module"] == m for p in selected), services[m],
            min(rank for rank, p in pool if p["module"] == m), m))
        _, page = min((rank, p) for rank, p in pool if p["module"] == choice)
        selected.append(page); best.pop(page["path"])
    return selected


def handoff_is_inline(journal: Any, prompt: dict, candidate: dict) -> bool:
    """Admission requires actual new source bytes in this exact bounded prompt."""
    witness = prompt.get("coding_handoff", {})
    if (witness.get("version") != HANDOFF_VERSION or witness.get("evidence") != candidate
            or witness.get("authorization_hash") != journal.spec().binding.authorization_hash
            or content_hash(prompt.get("binding")) != content_hash(journal.spec().binding.model_dump())):
        return False
    original = source_page(journal.wb.read(candidate["receipt_artifact"]), candidate["receipt_artifact"])
    if original is None:
        return False
    for page in prompt.get("source_working_set", []):
        if (page.get("receipt_artifact") == candidate["receipt_artifact"]
                and page.get("path") == candidate["path"]
                and page.get("source_hash") == candidate["source_hash"]
                and page.get("content_hash") == candidate["content_hash"]
                and page.get("source_page_artifact") == original["source_page_artifact"]
                and page.get("total_bytes") == original["total_bytes"] and isinstance(page.get("text"), str)):
            if type(page.get("offset")) is not int:
                return False
            start, end = page["offset"], page["offset"] + len(page["text"].encode())
            if page.get("next_offset") != (end if end < original["total_bytes"] else None):
                return False
            local = start - original["offset"]
            if local < 0 or original["text"].encode()[local:local + end - start] != page["text"].encode():
                return False
            actual = merge([[max(start, lo), min(end, hi)] for lo, hi in candidate["ranges"]
                if max(start, lo) < min(end, hi)])
            if actual and witness.get("inline_ranges") == actual:
                return True
    return False


def task_brief(task: dict, artifact: str) -> dict:
    """Exact field extraction, never a model/platform-written task summary."""
    plan, issue = task["approved_plan"], task["issue"]
    return {"partial": True, "artifact_id": artifact, "binding_hash": task["binding_hash"],
        "brief": {k: v for k, v in plan.items() if k not in {
            "commands_to_run", "files_likely_to_change", "files_to_inspect"}},
        "issue": {k: issue[k] for k in ("title", "html_url", "number") if k in issue},
        "instruction": "Exact frozen Plan fields; goal/checks/write authority also appear at the root. Full Plan and Issue body remain in artifact_id. Recover missing details only when needed. Working-set bytes come from receipt_artifact; do not patch without sufficient evidence."}
