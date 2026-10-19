"""Data-only proposal failures and feedback; invalid drafts never change files."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Sequence
from app.security import SensitiveDataError, ensure_no_sensitive_data

MESSAGES = {
    "edit_anchor_missing":"修改草案引用的原文不存在，尚未修改代码。",
    "edit_anchor_ambiguous":"修改草案引用的原文有多处匹配，尚未修改代码。",
    "proposal_structure_invalid":"修改草案格式未通过校验，尚未修改代码。",
    "proposal_scope_invalid":"修改草案超出批准范围，已拒绝全部改动。",
    "proposal_hash_mismatch":"修改草案的文件哈希与冻结原文不符，尚未修改代码。",
    "proposal_sensitive_output":"修改草案含敏感内容，已拒绝保存和执行。",
    "proposal_budget_exceeded":"修改草案超过大小限制，尚未修改代码。",
}


class ProposalValidationError(ValueError):
    def __init__(self, code: str, *, path: str | None = None, operation_index: int | None = None,
                 edit_index: int | None = None, match_count: int | None = None,
                 anchor_hash: str | None = None, excerpts: list[dict] | None = None) -> None:
        super().__init__(MESSAGES[code])
        self.code=code
        self.safe_message=MESSAGES[code]
        self.retryable=code in {"edit_anchor_missing","edit_anchor_ambiguous","proposal_structure_invalid","proposal_hash_mismatch"}
        self.diagnostic={"version":"proposal-validation-v1","code":code,"path":path,
            "operation_index":operation_index,"edit_index":edit_index,
            "match_count":match_count,"anchor_hash":anchor_hash,"source_excerpts":excerpts or []}
        ensure_no_sensitive_data(self.diagnostic,context="proposal diagnostic")


def check_proposal_structure(raw: str, context: Any, allowed_paths: Sequence[str]) -> str:
    from app.providers.nvidia_nim import normalize_completion_content
    if not isinstance(raw,str) or len(raw.encode())>4_000_000:
        raise ProposalValidationError('proposal_budget_exceeded')
    try: ensure_no_sensitive_data(raw,context='proposal draft')
    except SensitiveDataError: raise ProposalValidationError('proposal_sensitive_output') from None
    raw=normalize_completion_content(raw)
    try: value=json.loads(raw)
    except (ValueError,TypeError): raise ProposalValidationError('proposal_structure_invalid') from None
    if not isinstance(value,dict): raise ProposalValidationError('proposal_structure_invalid')
    operations=value.get('operations')
    if not isinstance(operations,list) or not 1<=len(operations)<=64:
        raise ProposalValidationError('proposal_structure_invalid')
    entries={e.path:e for e in context.entries}; seen=set()
    for index,operation in enumerate(operations):
        if not isinstance(operation,dict): raise ProposalValidationError('proposal_structure_invalid',operation_index=index)
        path=operation.get('path')
        if not isinstance(path,str) or path not in allowed_paths or path not in entries or path in seen:
            raise ProposalValidationError('proposal_scope_invalid',operation_index=index)
        seen.add(path); entry=entries[path]
        if set(operation)-{'path','kind','expected_prior_hash','content','executable','edits'}:
            raise ProposalValidationError('proposal_structure_invalid',path=path,operation_index=index)
        if operation.get('kind') not in {'edit','write','append','delete'}:
            raise ProposalValidationError('proposal_structure_invalid',path=path,operation_index=index)
        if operation.get('expected_prior_hash')!=entry.prior_hash:
            raise ProposalValidationError('proposal_hash_mismatch',path=path,operation_index=index)
        if operation['kind']=='write' and (not isinstance(operation.get('content'),str)
                or (operation.get('executable') is not None and type(operation.get('executable')) is not bool)
                or operation.get('edits') is not None):
            raise ProposalValidationError('proposal_structure_invalid',path=path,operation_index=index)
        if operation['kind']=='delete' and any(operation.get(key) is not None for key in ('content','edits','executable')):
            raise ProposalValidationError('proposal_structure_invalid',path=path,operation_index=index)
        if operation['kind']=='append' and (entry.content is None or not isinstance(operation.get('content'),str)
                or not operation['content'] or operation.get('executable') is not None):
            raise ProposalValidationError('proposal_structure_invalid',path=path,operation_index=index)
    return raw


def anchor_failure(content: str, old: str, *, path: str | None, operation_index: int | None,
                   edit_index: int, match_count: int) -> ProposalValidationError:
    """Excerpts are suggestions to the model, never fuzzy replacement authority."""
    tokens=sorted(set(re.findall(r'[A-Za-z_][A-Za-z0-9_]{3,}',old)),key=lambda t:(-len(t),t))[:16]
    lines=content.splitlines(keepends=True)
    excerpts=[]
    for token in tokens:
        matches=[i for i,line in enumerate(lines) if token in line]
        if not matches: continue
        for index in matches[:3]:
            start=max(0,index-2); text=''.join(lines[start:index+3])[:1500]
            excerpts.append({'start_line':start+1,'text':text})
        break
    return ProposalValidationError('edit_anchor_missing' if match_count==0 else 'edit_anchor_ambiguous',
        path=path,operation_index=operation_index,edit_index=edit_index,
        match_count=min(match_count,1_000_000),anchor_hash=hashlib.sha256(old.encode()).hexdigest(),excerpts=excerpts)
