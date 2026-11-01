"""Page background planning facts before shrinking the current source response."""
from collections.abc import Callable
from typing import Any

from app.planning_delivery import HARD_PROMPT_BYTES
from app.provenance import canonical_json, content_hash


def page_background(body: dict, original: dict, reference: Callable[..., str]) -> None:
    """Only the caller's projected copy changes; every omitted value is indexed."""
    def size() -> int:
        return len(canonical_json(body).encode())

    def indexed(value: Any, label: str) -> str:
        ref = reference(value, label)
        manifest = body['context_management']['references']
        if not any(item['reference_id'] == ref for item in manifest):
            manifest.append({'reference_id': ref, 'label': label, 'source_hash': content_hash(value),
                'bytes': len(canonical_json(value).encode())})
        return ref

    if size() <= HARD_PROMPT_BYTES:
        return
    # Preserve the first exact text and every occurrence's chronological place
    # and source event/hash. Only identical complete instructions can share it.
    seen: dict[str, dict] = {}
    for item in body.get('task_memory', {}).get('user_instructions', []):
        text = item.get('text')
        if not isinstance(text, str) or item.get('partial') or len(text.encode()) < 256:
            continue
        if text not in seen:
            seen[text] = item
            continue
        ref = indexed({'text': text}, 'repeated exact user instruction')
        item.pop('text')
        item.update(reference_id=ref, same_text_as_event_id=seen[text]['event_id'],
            text_indexed=True, partial=True)
    conversation = body.get('conversation', [])
    for index, item in enumerate(conversation[:-2]):
        if size() <= HARD_PROMPT_BYTES:
            break
        if len(canonical_json(item).encode()) > 512:
            ref = indexed(original['conversation'][index], 'historical conversation')
            conversation[index] = {'role': item['role'], 'reference_id': ref,
                'content_indexed': True, 'partial': True}
    # Exact originals already live behind history_reference. Keep selectors,
    # actual offsets and completeness, while paging duplicated delivery detail.
    for result in body.get('context_read_history', []):
        if result.get('text_indexed') and result.get('history_reference'):
            delivery = result.pop('delivery', None)
            if delivery:
                result['delivery_limited'] = delivery.get('limited')
                result['delivered_bytes'] = delivery.get('delivered_bytes')
            if result.get('read_request', {}).get('reference_id') == result.get('reference_id'):
                result.pop('reference_id', None)
    status, source = body.get('execution_status', {}), original.get('execution_status', {})
    snapshot = status.get('repository_snapshot') if isinstance(status, dict) else None
    if size() > HARD_PROMPT_BYTES and isinstance(snapshot, dict) and snapshot.get('files'):
        ref = indexed(source['repository_snapshot'], 'execution repository snapshot')
        snapshot.pop('files')
        snapshot.update(reference_id=ref, files_indexed=True, partial=True,
            read_hint='Use evidence.entries for captured paths and availability; read this reference for the full original snapshot. Capture is not proof of model inspection.')
    pairs = [(status, source)]
    if isinstance(status, dict) and isinstance(source, dict):
        pairs.append((status.get('preflight', {}), source.get('preflight', {})))
    for parent, original_parent in pairs:
        value = parent.get('environment') if isinstance(parent, dict) else None
        if size() <= HARD_PROMPT_BYTES:
            break
        if isinstance(value, dict) and len(canonical_json(value).encode()) > 1024:
            ref = indexed(original_parent['environment'], 'execution environment')
            parent['environment'] = {**{key: item for key, item in value.items() if not isinstance(item, (dict, list))},
                **({'verification_contract': value['verification_contract']} if 'verification_contract' in value else {}),
                'reference_id': ref, 'partial': True,
                'read_hint': 'Read the original environment before relying on omitted declarations. Static support is not preparation, baseline success or approval.'}
    evidence = body.get('evidence', {})
    originals = {entry['path']: entry for entry in original.get('evidence', {}).get('entries', [])}
    for entry in evidence.get('entries', []):
        if entry.get('context_reference') and entry.get('skeleton'):
            entry['content'] = '[Source indexed]'
    for entry in sorted(evidence.get('entries', []), key=lambda item: len(canonical_json(item).encode()), reverse=True):
        if size() <= HARD_PROMPT_BYTES:
            break
        path, text = entry['path'], entry.get('content')
        # Keep repository instructions inline; exact current reads are a
        # separate pinned field and never pass through this projection.
        name = path.rsplit('/', 1)[-1].lower()
        if (not isinstance(text, str) or len(text.encode()) < 256
                or name == 'agents.md' or name.startswith('contributing') or 'pull_request_template' in name):
            continue
        ref = reference(originals[path], 'repository file: ' + path, entry.get('prior_hash'))
        manifest = body['context_management']['references']
        if not any(item['reference_id'] == ref for item in manifest):
            manifest.append({'reference_id': ref, 'label': 'repository file: ' + path,
                'source_hash': entry['prior_hash'], 'bytes': len(canonical_json(originals[path]).encode())})
        entry.update(content='[Source indexed]', context_reference=ref, skeleton=True, partial=True,
            original_bytes=len(originals[path]['content'].encode()))
        entry.pop('read_hint', None)
