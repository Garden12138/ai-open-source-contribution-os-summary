from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import MappingProxyType

from app.providers.analysis_documents import (
    analysis_document_hash,
    render_analysis_comparison_markdown,
    render_analysis_markdown,
)
from app.providers.history import (
    AnalysisVersionComparison,
    AnalysisVersionDetail,
    AnalysisVersionSummary,
)

NOW = datetime(2026, 9, 6, 8, 0, tzinfo=UTC)


def _summary(version_id: str = "analysis-1") -> AnalysisVersionSummary:
    return AnalysisVersionSummary(
        id=version_id,
        job_id="job-1",
        opportunity_id=1,
        snapshot_id="snapshot-1",
        score_version_id="score-1",
        provider_name="provider-must-stay-hidden",
        model_name="model-must-stay-hidden",
        model_version="model-version-must-stay-hidden",
        analyze_output_schema_version="analysis-schema-v4",
        record_hash="a" * 64,
        created_at=NOW,
    )


def _analysis(*, summary: str = "修复导出流程并补齐回归测试。") -> dict[str, object]:
    return {
        "project_summary": "ContribOS 是一个本地优先的开源贡献工作台。",
        "requirement_summary": "Issue 要求把分析详情改为安全的中文 Markdown 报告。",
        "problem_summary": "旧的问题摘要不会重复展示。",
        "current_behavior": "当前行为不会进入精简报告。",
        "expected_behavior": "期望行为不会进入精简报告。",
        "acceptance_criteria": ["下载和页面展示使用同一份 Markdown。"],
        "missing_information": ["这个字段不会进入精简报告。"],
        "similar_issue_pr_evidence": [],
        "competition": {
            "level": "medium",
            "summary": "已有讨论，但没有明确实现者。",
            "signals": ["这个技术信号不会单独展示。"],
        },
        "estimated_effort": {
            "size": "m",
            "hours_min": 4,
            "hours_max": 8,
            "rationale": "需要同时调整服务端和原生页面。",
        },
        "bounty_basis": {
            "has_bounty": True,
            "amount_usd": 250,
            "basis": "Issue 正文明确标注赏金。",
        },
        "risks": [
            {
                "code": "internal.code.must_not_render",
                "summary": "需要保持历史版本兼容。",
                "severity": "high",
            }
        ],
        "confidence": 0.95,
        "recommendation": "pursue",
        "recommendation_summary": summary,
        "fit_reasons": ["与 Python 和原生 JavaScript 技术栈匹配。"],
        "next_steps": ["先运行聚焦测试。"],
        "maintainer_questions": ["是否接受向后兼容的新文档接口？"],
        "cited_evidence_ids": ["private-evidence-id"],
        "citation_map": {"private": ["private-evidence-id"]},
    }


def _detail(analysis: dict[str, object]) -> AnalysisVersionDetail:
    return AnalysisVersionDetail(
        summary=_summary(),
        content=MappingProxyType(
            {
                "analysis": analysis,
                "provider": {"name": "provider-must-stay-hidden"},
                "contracts": {"prompt": "prompt-must-stay-hidden"},
                "cited_evidence_ids": ["private-evidence-id"],
                "usage": {"input_tokens": 123},
                "hashes": {"record": "a" * 64},
            }
        ),
        source_context=MappingProxyType(
            {
                "repository_name": "fixture/contribos",
                "repository_url": "https://github.com/fixture/contribos",
                "issue_title": "Improve analysis report",
                "issue_url": "https://github.com/fixture/contribos/issues/7",
            }
        ),
    )


def test_analysis_markdown_is_concise_chinese_and_omits_technical_json() -> None:
    document = render_analysis_markdown(_detail(_analysis()))

    assert document.startswith("# 开源贡献机会分析\n")
    assert "## 项目介绍" in document
    assert "ContribOS 是一个本地优先的开源贡献工作台" in document
    assert "[fixture/contribos](https://github.com/fixture/contribos)" in document
    assert "## 需求内容" in document
    assert "Issue 要求把分析详情改为安全的中文 Markdown 报告" in document
    assert "## 综合分析" in document
    assert "结论：建议投入" in document
    assert "项目与需求的关联" in document
    assert "预计投入：中等；约 4–8 小时" in document
    assert "竞争程度：中" in document
    assert "赏金：$250" in document
    assert "高风险：需要保持历史版本兼容" in document
    assert "## 风险与验收" in document
    assert "## 行动建议" in document
    for hidden in (
        "当前行为不会进入精简报告",
        "期望行为不会进入精简报告",
        "这个字段不会进入精简报告",
        "internal.code.must_not_render",
        "private-evidence-id",
        "provider-must-stay-hidden",
        "prompt-must-stay-hidden",
        "input_tokens",
        "confidence",
        "citation_map",
    ):
        assert hidden not in document
    assert "{" not in document
    digest = analysis_document_hash(document)
    assert len(digest) == 64
    assert digest != analysis_document_hash(document + " ")


def test_analysis_markdown_preserves_historical_language_and_escapes_markup() -> None:
    analysis = _analysis(
        summary="Keep original English.\n## forged heading <script>alert(1)</script>"
    )

    document = render_analysis_markdown(_detail(analysis))

    assert "Keep original English." in document
    assert "\n## forged heading" not in document
    assert "\\#\\# forged heading" in document
    assert "\\<script\\>alert(1)\\</script\\>" in document


def test_legacy_analysis_is_explicitly_marked_for_regeneration() -> None:
    analysis = _analysis()
    analysis.pop("project_summary")
    analysis.pop("requirement_summary")
    detail = AnalysisVersionDetail(
        summary=replace(
            _summary(),
            analyze_output_schema_version="analysis-schema-v3",
        ),
        content=_detail(analysis).content,
    )

    document = render_analysis_markdown(detail)

    assert "历史版本说明" in document
    assert "重新生成关联分析" in document


def test_analysis_comparison_uses_semantic_chinese_sections_only() -> None:
    left = _analysis(summary="当前结论。")
    right = _analysis(summary="历史结论。")
    right["confidence"] = 0.1
    comparison = AnalysisVersionComparison(
        left_version_id="analysis-2",
        right_version_id="analysis-1",
        opportunity_id=1,
        same_snapshot=True,
        same_rule_score=True,
        same_frozen_input=True,
        left=MappingProxyType({"analysis": left, "hashes": {"record": "left"}}),
        right=MappingProxyType({"analysis": right, "hashes": {"record": "right"}}),
        differences=(),
    )

    document = render_analysis_comparison_markdown(comparison)

    assert document.startswith("# 分析版本对比\n")
    assert "## 结论摘要" in document
    assert "### 当前所选版本" in document
    assert "当前结论。" in document
    assert "### 对比版本" in document
    assert "历史结论。" in document
    assert "confidence" not in document
    assert "/analysis/" not in document
    assert "hashes" not in document
    assert "{" not in document


def test_analysis_comparison_ignores_technical_only_changes() -> None:
    analysis = _analysis()
    comparison = AnalysisVersionComparison(
        left_version_id="analysis-2",
        right_version_id="analysis-1",
        opportunity_id=1,
        same_snapshot=False,
        same_rule_score=False,
        same_frozen_input=False,
        left=MappingProxyType({"analysis": analysis, "hashes": {"record": "left"}}),
        right=MappingProxyType({"analysis": analysis, "hashes": {"record": "right"}}),
        differences=(),
    )

    assert render_analysis_comparison_markdown(comparison) == (
        "# 分析版本对比\n\n重要分析内容没有变化。\n"
    )
