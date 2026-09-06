from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.providers.history import AnalysisVersionComparison, AnalysisVersionDetail
from app.security import ensure_no_sensitive_data

ANALYSIS_DOCUMENT_VERSION = "analysis-document-v2"

_RECOMMENDATION_LABELS = {
    "pursue": "建议投入",
    "consider": "进一步确认后再决定",
    "skip": "暂不建议投入",
    "insufficient_evidence": "现有信息不足",
}
_LEVEL_LABELS = {
    "low": "低",
    "medium": "中",
    "high": "高",
    "unknown": "未知",
}
_EFFORT_LABELS = {
    "xs": "极小",
    "s": "较小",
    "m": "中等",
    "l": "较大",
    "xl": "很大",
    "unknown": "暂无法判断",
}
_MARKDOWN_INLINE_PUNCTUATION = re.compile(r"([\\`*_\[\]<>&#])")
_MARKDOWN_BLOCK_PREFIX = re.compile(r"^([+>]|-(?=\s)|\d+[.)](?=\s))")
_GITHUB_URL = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
    r"(?:/issues/[0-9]+)?/?$"
)


def render_analysis_markdown(detail: AnalysisVersionDetail) -> str:
    """Build the stable, user-facing Markdown projection of an analysis."""

    analysis = _mapping(detail.content.get("analysis"))
    recommendation = _RECOMMENDATION_LABELS.get(
        _string(analysis.get("recommendation")),
        "历史分析未提供明确建议",
    )
    summary = _string(analysis.get("recommendation_summary")) or _string(
        analysis.get("problem_summary")
    )
    project_summary = _string(analysis.get("project_summary"))
    requirement_summary = _string(analysis.get("requirement_summary"))
    source = detail.source_context

    lines = ["# 开源贡献机会分析"]
    if project_summary and requirement_summary:
        lines.extend(("", "## 项目介绍", "", _escape(project_summary)))
        _append_source_link(
            lines,
            "项目来源",
            _string(source.get("repository_name")) or "GitHub 项目",
            _string(source.get("repository_url")),
        )
        lines.extend(("", "## 需求内容", "", _escape(requirement_summary)))
        _append_source_link(
            lines,
            "需求来源",
            _string(source.get("issue_title")) or "GitHub Issue",
            _string(source.get("issue_url")),
        )
    else:
        lines.extend(
            (
                "",
                "历史版本说明：这份分析生成于关联性增强之前，未直接使用项目介绍和需求内容作为正式分析输入。请为当前机会重新生成关联分析。",
            )
        )

    lines.extend(("", "## 综合分析", "", f"结论：{_escape(recommendation)}。"))
    if summary:
        lines.append(_escape(summary))

    fit_reasons = _strings(analysis.get("fit_reasons"))
    if fit_reasons:
        lines.extend(
            (
                "",
                "项目与需求的关联："
                + _escape("；".join(_without_terminal(value) for value in fit_reasons))
                + "。",
            )
        )

    judgment = _judgment_lines(analysis)
    if judgment:
        lines.extend(("", "".join(_sentence(value) for value in judgment)))

    risks = _risk_lines(analysis.get("risks"))
    acceptance = _strings(analysis.get("acceptance_criteria"))
    if risks or acceptance:
        lines.extend(("", "## 风险与验收", ""))
        lines.extend(f"- 风险：{_escape(value)}" for value in risks)
        lines.extend(f"- 验收：{_escape(value)}" for value in acceptance)

    next_steps = _strings(analysis.get("next_steps"))
    questions = _strings(analysis.get("maintainer_questions"))
    if next_steps or questions:
        lines.extend(("", "## 行动建议", ""))
        lines.extend(f"- 下一步：{_escape(value)}" for value in next_steps)
        lines.extend(f"- 向维护者确认：{_escape(value)}" for value in questions)

    document = "\n".join(lines).rstrip() + "\n"
    ensure_no_sensitive_data(document, context="analysis Markdown document")
    return document


def render_analysis_comparison_markdown(
    comparison: AnalysisVersionComparison,
) -> str:
    """Render important semantic changes without exposing JSON paths or metadata."""

    left = _presentation_fields(_mapping(comparison.left.get("analysis")))
    right = _presentation_fields(_mapping(comparison.right.get("analysis")))
    lines = ["# 分析版本对比", ""]
    changed = False
    for key, label in _COMPARISON_FIELDS:
        left_values = left[key]
        right_values = right[key]
        if left_values == right_values:
            continue
        changed = True
        lines.extend((f"## {label}", "", "### 当前所选版本", ""))
        _append_values(lines, left_values)
        lines.extend(("", "### 对比版本", ""))
        _append_values(lines, right_values)
        lines.append("")
    if not changed:
        lines.append("重要分析内容没有变化。")

    document = "\n".join(lines).rstrip() + "\n"
    ensure_no_sensitive_data(document, context="analysis comparison Markdown")
    return document


def analysis_document_hash(document: str) -> str:
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


_COMPARISON_FIELDS = (
    ("project", "项目介绍"),
    ("requirement", "需求内容"),
    ("recommendation", "结论"),
    ("summary", "结论摘要"),
    ("fit", "为什么适合"),
    ("judgment", "投入与机会判断"),
    ("risks", "关键风险"),
    ("acceptance", "验收标准"),
    ("next_steps", "建议的下一步"),
    ("questions", "需要向维护者确认"),
)


def _presentation_fields(analysis: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    recommendation = _RECOMMENDATION_LABELS.get(
        _string(analysis.get("recommendation")),
        "历史分析未提供明确建议",
    )
    summary = _string(analysis.get("recommendation_summary")) or _string(
        analysis.get("problem_summary")
    )
    return {
        "project": (_string(analysis.get("project_summary")),)
        if _string(analysis.get("project_summary"))
        else (),
        "requirement": (_string(analysis.get("requirement_summary")),)
        if _string(analysis.get("requirement_summary"))
        else (),
        "recommendation": (recommendation,),
        "summary": (summary,) if summary else (),
        "fit": _strings(analysis.get("fit_reasons")),
        "judgment": _judgment_lines(analysis),
        "risks": _risk_lines(analysis.get("risks")),
        "acceptance": _strings(analysis.get("acceptance_criteria")),
        "next_steps": _strings(analysis.get("next_steps")),
        "questions": _strings(analysis.get("maintainer_questions")),
    }


def _append_source_link(
    lines: list[str],
    prefix: str,
    label: str,
    url: str,
) -> None:
    if _GITHUB_URL.fullmatch(url):
        lines.extend(("", f"{prefix}：[{_escape(label)}]({url})"))


def _judgment_lines(analysis: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    effort = _mapping(analysis.get("estimated_effort"))
    if effort:
        effort_parts = [
            "预计投入：" + _EFFORT_LABELS.get(_string(effort.get("size")), "暂无法判断")
        ]
        minimum = _integer(effort.get("hours_min"))
        maximum = _integer(effort.get("hours_max"))
        if minimum is not None and maximum is not None:
            effort_parts.append(
                f"约 {minimum} 小时"
                if minimum == maximum
                else f"约 {minimum}–{maximum} 小时"
            )
        rationale = _string(effort.get("rationale"))
        if rationale:
            effort_parts.append(rationale)
        values.append("；".join(effort_parts))

    competition = _mapping(analysis.get("competition"))
    if competition:
        value = "竞争程度：" + _LEVEL_LABELS.get(
            _string(competition.get("level")), "未知"
        )
        summary = _string(competition.get("summary"))
        values.append(f"{value}；{summary}" if summary else value)

    bounty = _mapping(analysis.get("bounty_basis"))
    if bounty:
        if bounty.get("has_bounty") is True:
            amount = bounty.get("amount_usd")
            value = "赏金：金额待确认"
            if isinstance(amount, (int, float)) and not isinstance(amount, bool):
                value = f"赏金：${amount:,.2f}".removesuffix(".00")
        else:
            value = "赏金：没有明确赏金"
        basis = _string(bounty.get("basis"))
        values.append(f"{value}；{basis}" if basis else value)
    return tuple(values)


def _risk_lines(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    result: list[str] = []
    for item in value:
        risk = _mapping(item)
        summary = _string(risk.get("summary"))
        if not summary:
            continue
        severity = _LEVEL_LABELS.get(_string(risk.get("severity")), "未知")
        result.append(f"{severity}风险：{summary}")
    return tuple(result)


def _append_list_section(
    lines: list[str],
    heading: str,
    values: Sequence[str],
) -> None:
    if not values:
        return
    lines.extend(("", f"## {heading}", ""))
    lines.extend(f"- {_escape(value)}" for value in values)


def _append_values(lines: list[str], values: Sequence[str]) -> None:
    if not values:
        lines.append("未提供。")
    elif len(values) == 1:
        lines.append(_escape(values[0]))
    else:
        lines.extend(f"- {_escape(value)}" for value in values)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(text for item in value if (text := _string(item)))


def _string(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).strip()


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _without_terminal(value: str) -> str:
    return _string(value).rstrip("。！？.!?")


def _sentence(value: str) -> str:
    text = _string(value)
    suffix = "" if text.endswith(("。", "！", "？", ".", "!", "?")) else "。"
    return _escape(text) + suffix


def _escape(value: str) -> str:
    escaped = _MARKDOWN_INLINE_PUNCTUATION.sub(r"\\\1", _string(value))
    return _MARKDOWN_BLOCK_PREFIX.sub(r"\\\1", escaped)
