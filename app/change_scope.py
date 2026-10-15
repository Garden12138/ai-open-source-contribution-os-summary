"""Conservative discovery hints; these never establish actual diff size or readiness."""
from __future__ import annotations

import re
from typing import Any

RULE_VERSION = "change-scope-v2"


def assess_change_scope(
    *, title: str, body: str, labels: list[str], effort: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify frozen Issue evidence without changing scores or invoking a model."""
    truncated = len(title) > 1000 or len(body) > 60000
    title = title[:1000].casefold()
    text = title + "\n" + body[:60000].casefold()
    # GitHub size labels commonly omit spaces or use slash separators.
    labels = {re.sub(r"\s*[:/]\s*", ":", label.casefold().strip()) for label in labels[:100]}
    reasons: list[str] = []
    exclusions: list[str] = []
    if truncated:
        exclusions.append("scope_text_truncated")

    def matches(pattern: str, value: str = text) -> bool:
        return re.search(pattern, value) is not None

    text_fix = matches(r"\b(?:typos?|misspell(?:ing|ed)?|spelling|broken links?|dead links?)\b|错别字|拼写|失效链接|文案错误", title) and matches(r"\b(?:fix|correct|remove|repair|typos?)\b|修复|更正|纠正", title)
    broad_title = matches(r"\b(?:migrate|migration|rewrite|redesign|rearchitect|overhaul|epic)\b|迁移|重写|重新设计|全面重构|架构改造", title)
    broad_body = matches(r"\b(?:rewrite|migrate|refactor|redesign)\s+(?:all|the entire|the whole)|\bbreaking changes?\b|\bacross (?:all |multiple )?(?:modules|packages|repositories)\b|\b(?:database|schema) migration\b|跨模块|跨仓库|全量迁移|重构所有")
    broad_label = bool(labels & {"epic", "breaking change", "size:l", "size:xl", "size:large"})
    # A typo in a migration guide is still a text fix; explicit broad work wins.
    doc_reference = text_fix and matches(r"\b(?:typo|spelling).{0,30}\b(?:in|within)\b.{0,40}\bmigration (?:guide|docs|documentation)\b", title)
    if (broad_title and not doc_reference) or broad_body or broad_label:
        exclusions.append("large_scope")
    if matches(r"\b(?:e2e|end.to.end|browser|acceptance|stress) tests?\b|\bplaywright\b|\bpuppeteer\b|浏览器测试|端到端测试|压测", title) or matches(
        r"\brequires?\s+(?:a |an )?(?:running |local )?(?:database|redis|docker|cluster)|\b(?:100|200)\s+(?:desktop|mobile|runs|times)|需(?:要)?(?:启动|运行).{0,12}(?:服务|数据库)|启动.{0,8}(?:redis|应用服务)"
    ):
        exclusions.append("complex_verification")
    if effort:
        hours = effort.get("hours_max")
        if effort.get("size") in {"m", "l", "xl"} or (isinstance(hours, (int, float)) and not isinstance(hours, bool) and hours > 8):
            exclusions.append("high_estimated_effort")
        elif effort.get("size") in {"xs", "s"} and isinstance(hours, (int, float)) and not isinstance(hours, bool) and 0 < hours <= 4:
            reasons.append("small_effort_estimate")
    if text_fix:
        reasons.append("text_or_link_fix")
    # Require a narrow requested edit and a concrete documentation/metadata target.
    # A documentation label or generic 'improve docs' alone is insufficient.
    if matches(r"\b(?:add|update|fix|correct)\b.{0,80}\b(?:links?|screenshots?)\b.{0,40}\b(?:readme|docs?|documentation)\b", title) and matches(r"\breadme\b|\.md\b"):
        reasons.append("localized_documentation")
    if matches(r"\badd\b.{0,80}\blinks?\b.{0,40}\bpypi (?:project )?page\b", title) and matches(r"\[project\.urls\]") and matches(r"\bpyproject\.toml\b"):
        reasons.append("package_metadata_links")
    if matches(r"\b(?:off.by.one|wrong (?:argument|parameter|default)|incorrect (?:error )?message|missing (?:null )?(?:check|guard)|empty (?:input|string))\b|参数错误|错误提示|边界条件|空值处理", title):
        reasons.append("localized_fix")
    if matches(r"\b(?:single (?:file|function|method)|one (?:file|function|method)|two files|three files|[1-3] files)\b|单个(?:文件|函数)|[一二三123]个文件"):
        reasons.append("bounded_scope")
    if labels & {"size:xs", "size:s", "size:small", "effort:small", "small change"}:
        reasons.append("small_size_label")
    if labels & {"good first issue", "good-first-issue", "beginner friendly", "first-timers-only"}:
        reasons.append("beginner_friendly")
    substantive = set(reasons) - {"beginner_friendly"}
    classification = "excluded" if exclusions else "likely_small" if substantive else "unknown"
    return {"rule_version": RULE_VERSION, "classification": classification,
            "reason_codes": reasons, "exclusion_codes": exclusions or ([] if substantive else ["insufficient_scope_evidence"])}
