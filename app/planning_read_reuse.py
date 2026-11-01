"""Reuse admitted repository pages only under the same frozen source binding."""
from copy import deepcopy

from app.artifacts import ArtifactError
from app.context_compaction import EVENT, VERSION, source_binding
from app.jobs import TERMINAL_STATES
from app.models import Job
from app.provenance import canonical_json, content_hash
from app.security import ensure_no_sensitive_data
from app.workbench import Workbench, WorkbenchError


def _receipt(wb: Workbench, task_id: str, checkpoint_hash: str,
             model_profile_id: str | None) -> tuple[list[dict], dict] | None:
    history = wb.history(task_id)
    event = next((e for e in reversed(history) if e.kind == EVENT and e.actor_id == 'orchestrator'
        and e.payload.get('scope') == 'planning' and e.payload.get('artifact_id') == checkpoint_hash), None)
    if event is None:
        return None
    call = next((e for e in history if e.kind == 'model_call' and e.actor_id == 'system'
        and e.job_id == event.job_id and e.payload.get('context_checkpoint_hash') == checkpoint_hash), None)
    job = wb.session.get(Job, event.job_id)
    if (call is None or job is None or job.kind != 'planning_turn' or job.state not in TERMINAL_STATES
            or job.payload.get('agent_runtime_version') != 'agent-runtime-v2'
            or call.payload.get('policy') != 'planning-v5-independent-actions'
            or job.payload.get('model_profile_id') != model_profile_id):
        return None
    try:
        checkpoint = wb.read(checkpoint_hash)
    except ArtifactError:
        raise WorkbenchError('既有源码读取收据校验失败。', code='planning_read_reuse_invalid') from None
    if (checkpoint.get('version') != VERSION or checkpoint.get('scope') != 'planning'
            or checkpoint.get('prompt_hash') != content_hash(canonical_json(checkpoint.get('prompt')))
            or checkpoint.get('prompt_hash') != call.payload.get('input_hash')
            or checkpoint.get('prompt_hash') != event.payload.get('prompt_hash')
            or checkpoint.get('source') != event.payload.get('source')):
        raise WorkbenchError('既有源码读取收据校验失败。', code='planning_read_reuse_invalid')
    if (checkpoint['source'] != source_binding(wb, task_id)
            or wb.models_changed_after(task_id, checkpoint['source_cursor'])
            or any(e.kind == 'stop_requested' and e.sequence > checkpoint['source_cursor'] for e in history)):
        return None
    context = wb.latest(task_id, 'context_ready')
    evidence = wb.read(context.payload['artifact_id'])
    available = {e['path']: e.get('prior_hash') for e in evidence['entries'] if e.get('content') is not None}
    pages = [deepcopy(page) for page in checkpoint['prompt'].get('context_reads', [])
        if isinstance(page, dict) and page.get('source_path') in available
        and page.get('source_hash') == available[page['source_path']] and isinstance(page.get('text'), str)]
    if not pages or len(pages) > 4:
        return None
    ensure_no_sensitive_data(pages, context='reused repository pages')
    return pages, {'version': 'planning-read-reuse-v1', 'checkpoint_hash': checkpoint_hash,
        'source_job_id': job.id, 'source_prompt_hash': checkpoint['prompt_hash'],
        'repository_reads_executed': 0,
        'notice': 'These are earlier admitted repository tool responses for this exact frozen binding, not new repository reads or execution evidence. Reuse their actual text; partial pages never prove omitted source.'}


def prior_read_checkpoint(wb: Workbench, task_id: str, model_profile_id: str | None) -> str | None:
    """Select an issued, model-admitted receipt; no new artifact or authority."""
    for event in reversed(wb.history(task_id)):
        if event.kind == EVENT and event.actor_id == 'orchestrator' and event.payload.get('scope') == 'planning':
            ref = event.payload['artifact_id']
            if _receipt(wb, task_id, ref, model_profile_id) is not None:
                return ref
    return None


def reused_read_pages(wb: Workbench, task_id: str, checkpoint_hash: str,
                      model_profile_id: str | None) -> tuple[list[dict], dict]:
    value = _receipt(wb, task_id, checkpoint_hash, model_profile_id)
    if value is None:
        raise WorkbenchError('既有源码读取依据已变化。', code='planning_read_reuse_stale')
    return value
