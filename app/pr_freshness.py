"""Current remote questions require a read after the question, never a cached answer."""
from __future__ import annotations

import re
from typing import Any


def saved_status_requested(text: str) -> bool:
    if re.search(r"(?:不要|不用|不需要|别|暂不).{0,8}(?:刷新|联网|查询|跟进)|(?:do not|don't).{0,12}(?:refresh|fetch|query)",text,re.I):
        return True
    if re.search(r"(?:不要|不用|别).{0,8}(?:缓存|旧|历史)|实时|刷新|重新查询|latest status|live",text,re.I):
        return False
    return bool(re.search(r"上次|之前|历史|当时|缓存|保存的|已保存|cached|saved|historical|last observed", text, re.I))


def current_status_requested(text: str) -> bool:
    """Only selects a credential-separated GET workflow, never a write."""
    if saved_status_requested(text) or re.search(r"不要|不用|不需要|别查|暂不|don.t|do not",text,re.I):
        return False
    if not re.search(r"(?<![a-z0-9_])pr(?![a-z0-9_])|pull\s*request|合并|维护者|审核|(?<![a-z])ci(?![a-z])",text,re.I):
        return False
    if re.search(r"链接|地址|url|link",text,re.I) and not re.search(r"状态|情况|进展|合并|关闭|\bci\b|检查",text,re.I):
        return False
    if re.search(r"预览|文案|正文|标题|diff|方案|preview|title|body",text,re.I):
        return False
    # An explicit state-changing request must keep its separate preview route.
    if re.search(r"完成|提交|转为|取消草稿|prepare|mark.*ready",text,re.I):
        return False
    return bool(re.search(r"现在|当前|最新|情况|状态|进展|合并了|跟进|查看|看看|看下|检查|核对|查询|刷新|同步|status|progress|latest|current|check",text,re.I))


def answer_requires_refresh(wb: Any, task_id: str, message: Any, references: list[dict]) -> bool:
    """Default PR-status citations are current; historical reads must be explicit."""
    if not wb.latest(task_id,"publication_completed") or saved_status_requested(message.payload["text"]):
        return False
    cited = [r for r in references if r.get("name") == "pull_request"]
    if not cited:
        return False
    observation = wb.latest(task_id,"pr_followup_observed")
    if observation is None:
        return current_status_requested(message.payload["text"])
    return (observation.sequence <= message.sequence
            or any(r.get("source_hash") != observation.record_hash for r in cited))
