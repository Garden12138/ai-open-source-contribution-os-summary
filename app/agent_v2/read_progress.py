"""Bound repeated native source reads using committed, model-visible evidence."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from app.agent_v2.contracts import Hash, ReadFile, StrictModel, ToolResult
from app.provenance import content_hash
from app.workbench import WorkbenchError

if TYPE_CHECKING:
    from app.agent_v2.journal import AgentJournal

VERSION = 'source-read-progress-v1'
VISIBLE_WARNING_LIMIT = 2
POLICY = {'version': VERSION, 'visible_duplicate_receipt_limit': VISIBLE_WARNING_LIMIT,
    'binding': 'workspace_file_hash_path_exact_byte_request',
    'warning_source': 'distinct_receipts_in_identity_confirmed_native_prompts',
    'action': 'block_before_repeating_gateway_keep_all_charges'}


class ReadProgressNote(StrictModel):
    version: Literal['source-read-progress-v1'] = VERSION
    range_key: Hash
    max_bytes: int = Field(ge=128, le=16000)
    prior_same_range_reads: int = Field(ge=0, le=256)
    prior_receipt: Hash | None
    warning_receipts_seen: list[Hash] = Field(max_length=VISIBLE_WARNING_LIMIT)
    visible_bytes: int = Field(ge=0, le=16000)
    visible_next_offset: int | None = Field(ge=0, le=1000000)
    instruction: str = ('Follow the visible next_offset for further source bytes. Reuse the prior receipt with '
        'read_artifact when needed. Repeating an unchanged byte request after two visible duplicate receipts stops '
        'the session; no arguments, scope or budget are changed.')


@dataclass(frozen=True)
class ReadProgress:
    note: ReadProgressNote
    file_hash: str

    @property
    def blocked(self) -> bool:
        return len(self.note.warning_receipts_seen) >= VISIBLE_WARNING_LIMIT


def _page(result: ToolResult, path: str) -> bool:
    d = result.data
    if (result.status != 'succeeded' or d.get('tool') != 'read_file'
            or d.get('execution_backend') not in {'docker', 'platform_receipt_reuse'} or d.get('path') != path):
        return False
    if (type(d.get('offset')) is not int or type(d.get('total_bytes')) is not int
            or not 0 <= d['offset'] <= d['total_bytes'] <= 1000000
            or not isinstance(d.get('text'), str) or not isinstance(d.get('content_hash'), str)
            or len(d['content_hash']) != 64 or any(c not in '0123456789abcdef' for c in d['content_hash'])):
        return False
    end = d['offset'] + len(d['text'].encode())
    return end <= d['total_bytes'] and d.get('next_offset') == (end if end < d['total_bytes'] else None)


def _key(source_hash: str, file_hash: str, args: ReadFile) -> str:
    return content_hash({'version': VERSION, 'source_hash': source_hash, 'file_hash': file_hash,
        'path': args.path, 'offset': args.offset, 'max_bytes': args.max_bytes})


def source_read_progress(journal: AgentJournal, args: ReadFile, *, current: ToolResult | None = None) -> ReadProgress | None:
    """Metadata from real receipts; never rewrite requested offsets or execute code."""
    if journal.spec().model_transport is None:
        return None
    source_hash = current.output_source_hash if current else journal.checkpoint().source_hash
    rows = []
    for event in journal.events():
        if event.kind != 'agent_v2_tool_result' or event.payload.get('name') != 'read_file':
            continue
        raw = journal.wb.read(event.payload['artifact_id'])
        result = ToolResult.model_validate(raw)
        if result.output_source_hash != source_hash or not _page(result, args.path):
            continue
        data = result.data
        if 'source_read_progress' not in data:
            continue  # Older receipts cannot invent the original request's size.
        try:
            note = ReadProgressNote.model_validate(data['source_read_progress'])
            previous_args = ReadFile(path=data['path'], offset=data['offset'], max_bytes=note.max_bytes)
            if note.range_key != _key(source_hash, data['content_hash'], previous_args):
                raise ValueError('read_progress_binding_invalid')
        except (ValueError, TypeError):
            raise WorkbenchError('源码读取进度证据无效。', code='agent_source_read_progress_invalid') from None
        if (data['offset'] == args.offset and note.max_bytes == args.max_bytes
                and (args.expected_hash is None or args.expected_hash == data['content_hash'])):
            rows.append((event.payload['artifact_id'], result, note))
    if current is not None:
        if not _page(current, args.path):
            return None
        if current.data.get('source_read_progress') is not None or current.data['offset'] != args.offset:
            raise WorkbenchError('源码读取进度来源无效。', code='agent_source_read_progress_invalid')
        file_hash = current.data['content_hash']
    elif rows:
        file_hash = rows[-1][1].data['content_hash']
    else:
        return None
    key = _key(source_hash, file_hash, args)
    rows = [row for row in rows if row[2].range_key == key]
    by_id = {aid: (result, note) for aid, result, note in rows}
    warnings: list[str] = []
    visible_bytes, visible_next = 0, None
    from app.agent_v2.model_receipts import confirmed_prompts
    for _, prompt in confirmed_prompts(journal):
        shown = list(prompt.get('feedback', []))
        for page in prompt.get('source_working_set', []):
            aid = page.get('receipt_artifact')
            if aid not in by_id:
                continue
            raw, _ = by_id[aid]
            text, offset = page.get('text'), page.get('offset')
            if not isinstance(text, str) or type(offset) is not int:
                continue
            start = offset - raw.data['offset']
            if start < 0 or raw.data['text'].encode()[start:start + len(text.encode())] != text.encode():
                continue
            # The exact note must be actually present, not merely reachable by
            # hash. Its receipt binds the file hash and current workspace.
            shown.append({'receipt_artifact': aid, 'output_source_hash': page.get('output_source_hash'),
                'data': {**page, 'content_hash': raw.data['content_hash']}})
        for projected in shown:
            aid = projected.get('receipt_artifact')
            if aid not in by_id:
                continue
            raw, note = by_id[aid]
            data = projected.get('data', {})
            projected_note = data.get('source_read_progress')
            full_note = note.model_dump()
            compact_note = {k: v for k, v in full_note.items() if k != 'instruction'}
            if (projected_note not in (full_note, compact_note)
                    or projected.get('output_source_hash') != source_hash):
                continue
            if (data.get('path') == args.path and data.get('content_hash') == file_hash
                    and data.get('offset') == args.offset and isinstance(data.get('text'), str)):
                size = len(data['text'].encode())
                end = args.offset + size
                if size and end <= raw.data['total_bytes'] and data.get('next_offset') == (end if end < raw.data['total_bytes'] else None):
                    visible_bytes, visible_next = size, data['next_offset']
            if note.prior_same_range_reads > 0 and aid not in warnings:
                warnings.append(aid)
    note = ReadProgressNote(range_key=key, max_bytes=args.max_bytes, prior_same_range_reads=len(rows),
        prior_receipt=rows[-1][0] if rows else None, warning_receipts_seen=warnings[:VISIBLE_WARNING_LIMIT],
        visible_bytes=visible_bytes, visible_next_offset=visible_next)
    return ReadProgress(note, file_hash)
