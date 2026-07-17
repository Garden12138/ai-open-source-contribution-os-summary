(function () {
  "use strict";

  const API = {
    meta: "/api/v1/meta",
    daily: "/api/v1/opportunities/daily?limit=10",
    scans: "/api/v1/scans",
  };

  const REASONS = {
    bounty: { label: "赏金优先", short: "赏金" },
    strategic: { label: "战略匹配", short: "战略" },
    tech_match: { label: "技术栈匹配", short: "技术" },
    high_impact: { label: "高影响力", short: "影响力" },
    best_available: { label: "综合优选", short: "优选" },
  };

  const COMPONENT_LABELS = {
    reward_reliability: "回报可靠性",
    acceptance_probability: "接收概率",
    tech_match: "技术匹配",
    project_impact: "项目影响力",
    issue_clarity: "议题清晰度",
    competition: "竞争程度",
    learning_value: "学习价值",
  };

  const RISK_LABELS = {
    scope_may_be_too_large: "任务范围可能过大",
    high_discussion_or_competition: "讨论较多，竞争可能较高",
    issue_is_very_old: "议题创建时间较久",
    repository_language_unknown: "仓库主要语言未知",
    bounty_amount_or_terms_unclear: "赏金金额或条款不明确",
  };

  const state = {
    picks: [],
    activeFilter: "all",
    toastTimer: null,
    scanning: false,
  };

  const dom = {};

  document.addEventListener("DOMContentLoaded", init);

  function init() {
    Object.assign(dom, {
      appName: document.querySelector("#app-name"),
      tokenStatus: document.querySelector("#token-status"),
      systemStatus: document.querySelector("#system-status"),
      tokenNotice: document.querySelector("#token-notice"),
      tokenNoticeTitle: document.querySelector("#token-notice-title"),
      tokenNoticeCopy: document.querySelector("#token-notice-copy"),
      metaSummary: document.querySelector("#meta-summary"),
      scanButton: document.querySelector("#scan-button"),
      scanLabel: document.querySelector("#scan-button .scan-label"),
      scanNote: document.querySelector("#scan-note"),
      selectionDate: document.querySelector("#selection-date"),
      candidateCount: document.querySelector("#candidate-count"),
      eligibleCount: document.querySelector("#eligible-count"),
      selectedCount: document.querySelector("#selected-count"),
      generatedTime: document.querySelector("#generated-time"),
      generatedDate: document.querySelector("#generated-date"),
      resultCount: document.querySelector("#result-count"),
      filterBar: document.querySelector("#filter-bar"),
      statePanel: document.querySelector("#state-panel"),
      opportunityList: document.querySelector("#opportunity-list"),
      filterEmpty: document.querySelector("#filter-empty"),
      toast: document.querySelector("#toast"),
    });

    dom.scanButton.addEventListener("click", runScan);
    dom.filterBar.addEventListener("click", handleFilterClick);

    loadInitialData();
  }

  async function requestJSON(url, options) {
    const requestOptions = options || {};
    const response = await fetch(url, {
      ...requestOptions,
      headers: { Accept: "application/json", ...requestOptions.headers },
    });

    const contentType = response.headers.get("content-type") || "";
    let payload = null;

    if (contentType.includes("application/json")) {
      payload = await response.json();
    } else {
      const text = await response.text();
      payload = text ? { detail: text } : null;
    }

    if (!response.ok) {
      const detail = payload && (payload.detail || payload.message || payload.error);
      throw new Error(detail || `请求失败（HTTP ${response.status}）`);
    }

    return payload || {};
  }

  async function loadInitialData() {
    setLeaderboardLoading();

    const [metaResult, dailyResult] = await Promise.allSettled([
      requestJSON(API.meta),
      requestJSON(API.daily),
    ]);

    if (metaResult.status === "fulfilled") {
      renderMeta(metaResult.value);
    } else {
      renderMetaError();
    }

    if (dailyResult.status === "fulfilled") {
      renderDaily(dailyResult.value);
    } else {
      renderLeaderboardError(dailyResult.reason);
    }
  }

  function renderMeta(meta) {
    const appName = cleanText(meta.app_name) || "开源机会雷达";
    const tokenConfigured = Boolean(meta.token_configured);
    const languages = toStringArray(meta.preferred_languages);
    const queryCount = Array.isArray(meta.queries) ? meta.queries.length : 0;

    dom.appName.textContent = appName;
    document.title = `${appName} · 每日机会榜`;

    dom.systemStatus.classList.remove("is-warning");
    dom.tokenNotice.classList.remove("is-loading", "is-warning", "is-error");

    if (tokenConfigured) {
      dom.systemStatus.classList.add("is-ready");
      dom.tokenStatus.textContent = "GitHub 数据源已就绪";
      dom.tokenNoticeTitle.textContent = "GitHub Token 已配置";
      dom.tokenNoticeCopy.textContent = "扫描可使用更高的 API 额度，适合持续更新候选池。";
    } else {
      dom.systemStatus.classList.remove("is-ready");
      dom.systemStatus.classList.add("is-warning");
      dom.tokenStatus.textContent = "当前为限额模式";
      dom.tokenNotice.classList.add("is-warning");
      dom.tokenNoticeTitle.textContent = "尚未配置 GitHub Token";
      dom.tokenNoticeCopy.textContent =
        "仍可尝试扫描，但容易触发 GitHub 访问频率限制。可设置 GITHUB_TOKEN 或 GH_TOKEN 后重启服务。";
    }

    const summaries = [];
    if (languages.length) summaries.push(`偏好语言：${languages.join(" / ")}`);
    if (queryCount) summaries.push(`${formatNumber(queryCount)} 条检索规则`);
    dom.metaSummary.textContent = summaries.join(" · ");
  }

  function renderMetaError() {
    dom.systemStatus.classList.remove("is-ready");
    dom.systemStatus.classList.add("is-warning");
    dom.tokenStatus.textContent = "配置状态未知";
    dom.tokenNotice.classList.remove("is-loading", "is-warning");
    dom.tokenNotice.classList.add("is-error");
    dom.tokenNoticeTitle.textContent = "暂时无法读取扫描配置";
    dom.tokenNoticeCopy.textContent = "榜单仍会继续加载；若扫描失败，请确认服务已正常启动。";
    dom.metaSummary.textContent = "";
  }

  function renderDaily(data) {
    const picks = Array.isArray(data.picks) ? data.picks : [];
    state.picks = picks;

    dom.candidateCount.textContent = formatNumber(toFiniteNumber(data.total_candidates, 0));
    dom.eligibleCount.textContent = formatNumber(toFiniteNumber(data.total_eligible, 0));
    dom.selectedCount.textContent = formatNumber(picks.length);
    dom.selectionDate.textContent = formatSelectionDate(data.selection_date);
    renderGeneratedAt(data.generated_at);
    updateFilterCounts(picks);

    dom.statePanel.replaceChildren();
    dom.opportunityList.replaceChildren();

    if (!picks.length) {
      state.activeFilter = "all";
      updateActiveFilterButton();
      renderEmptyState();
      dom.resultCount.textContent = "暂无入选机会";
      return;
    }

    const fragment = document.createDocumentFragment();
    picks.forEach((pick, index) => fragment.appendChild(buildOpportunityCard(pick, index)));
    dom.opportunityList.appendChild(fragment);
    applyFilter(state.activeFilter);
  }

  function renderGeneratedAt(value) {
    const date = parseDate(value);
    if (!date) {
      dom.generatedTime.textContent = "—";
      dom.generatedDate.textContent = "更新时间未知";
      return;
    }

    dom.generatedTime.textContent = new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(date);
    dom.generatedDate.textContent = new Intl.DateTimeFormat("zh-CN", {
      month: "long",
      day: "numeric",
      timeZoneName: "short",
    }).format(date);
  }

  function buildOpportunityCard(pick, index) {
    const opportunity = pick && pick.opportunity && typeof pick.opportunity === "object"
      ? pick.opportunity
      : {};
    const repository = opportunity.repository && typeof opportunity.repository === "object"
      ? opportunity.repository
      : {};
    const reason = cleanText(pick.selection_reason) || "best_available";
    const reasonMeta = REASONS[reason] || { label: humanize(reason), short: humanize(reason) };
    const rank = Math.max(1, Math.trunc(toFiniteNumber(pick.rank, index + 1)));
    const score = clamp(
      toFiniteNumber(pick.score_snapshot, toFiniteNumber(opportunity.score_total, 0)),
      0,
      100,
    );

    const card = element("article", "opportunity-card");
    card.dataset.reason = reason;
    card.setAttribute("aria-labelledby", `opportunity-title-${index}`);

    const rankColumn = element("div", "rank-column");
    const rankBadge = element("span", "rank-badge", String(rank));
    if (rank <= 3) rankBadge.classList.add("is-podium");
    rankBadge.setAttribute("aria-label", `第 ${rank} 名`);
    rankColumn.appendChild(rankBadge);

    const main = element("div", "opportunity-main");
    const topline = element("div", "card-topline");
    const reasonBadge = element("span", "reason-badge", reasonMeta.label);
    reasonBadge.dataset.reason = reason;
    topline.appendChild(reasonBadge);

    const repoName = cleanText(repository.full_name) || "未知仓库";
    const repoLink = link(repository.html_url, repoName, "repo-link");
    topline.appendChild(repoLink);
    main.appendChild(topline);

    const title = element("h3", "issue-title");
    title.id = `opportunity-title-${index}`;
    const issueTitle = cleanText(opportunity.title) || `Issue #${opportunity.issue_number || "—"}`;
    title.appendChild(link(opportunity.html_url, issueTitle, "issue-link"));
    main.appendChild(title);

    const description = cleanText(repository.description);
    if (description) {
      main.appendChild(element("p", "repository-description", description));
    }

    const labels = toStringArray(opportunity.labels);
    if (labels.length) {
      const labelRow = element("div", "label-row");
      labels.slice(0, 4).forEach((label) => labelRow.appendChild(element("span", "issue-label", label)));
      if (labels.length > 4) {
        labelRow.appendChild(element("span", "issue-label", `+${labels.length - 4}`));
      }
      main.appendChild(labelRow);
    }

    const signalRow = buildSignalRow(opportunity);
    if (signalRow.childElementCount) main.appendChild(signalRow);

    const risks = toStringArray(opportunity.risk_reasons);
    if (risks.length) {
      const riskText = risks.map((risk) => RISK_LABELS[risk] || humanize(risk)).join("；");
      main.appendChild(element("div", "risk-row", riskText));
    }

    main.appendChild(buildCardFooter(opportunity, repository));

    const scoreColumn = buildScoreColumn(score, opportunity.score_components);

    card.append(rankColumn, main, scoreColumn);
    return card;
  }

  function buildSignalRow(opportunity) {
    const row = element("div", "signal-row");

    if (opportunity.has_bounty) {
      const amount = toOptionalNumber(opportunity.bounty_amount_usd);
      const text = amount === null
        ? "含赏金"
        : `赏金 ${new Intl.NumberFormat("en-US", {
            style: "currency",
            currency: "USD",
            maximumFractionDigits: amount % 1 ? 2 : 0,
          }).format(amount)}`;
      const tag = element("span", "signal-tag is-bounty", text);
      row.appendChild(tag);
    }

    if (opportunity.is_strategic) {
      row.appendChild(element("span", "signal-tag", "战略方向"));
    }

    if (opportunity.is_tech_match) {
      row.appendChild(element("span", "signal-tag", "偏好技术栈"));
    }

    return row;
  }

  function buildCardFooter(opportunity, repository) {
    const footer = element("div", "card-footer");
    const facts = element("div", "repo-facts");

    const language = cleanText(repository.language) || "语言未知";
    facts.appendChild(element("span", "", `◉ ${language}`));
    facts.appendChild(element("span", "", `☆ ${formatCompact(repository.stars)} stars`));

    const license = cleanText(repository.license_spdx);
    if (license) facts.appendChild(element("span", "", `许可 ${license}`));

    const issueNumber = opportunity.issue_number;
    if (issueNumber !== null && issueNumber !== undefined) {
      facts.appendChild(element("span", "", `#${issueNumber}`));
    }

    facts.appendChild(
      element("span", "", `${formatNumber(toFiniteNumber(opportunity.comments_count, 0))} 条评论`),
    );

    const issueLink = link(opportunity.html_url, "查看 Issue ↗", "open-issue-link");
    footer.append(facts, issueLink);
    return footer;
  }

  function buildScoreColumn(score, components) {
    const column = element("div", "score-column");
    const ring = element("div", "score-ring");
    ring.style.setProperty("--score", score.toFixed(1));
    ring.setAttribute("role", "img");
    ring.setAttribute("aria-label", `综合评分 ${formatScore(score)} 分`);
    ring.appendChild(element("span", "score-number", formatScore(score)));

    column.append(ring, element("span", "score-caption", "综合评分 / 100"));

    const entries = components && typeof components === "object"
      ? Object.entries(components).filter((entry) => Number.isFinite(Number(entry[1])))
      : [];

    if (entries.length) {
      const details = element("details", "score-details");
      const summary = element("summary", "", "查看评分构成");
      const list = element("div", "component-list");

      entries.forEach(([key, value]) => {
        const numericValue = clamp(Number(value), 0, 100);
        const item = element("div", "component-item");
        item.append(
          element("span", "", COMPONENT_LABELS[key] || humanize(key)),
          element("strong", "", formatScore(numericValue)),
        );
        const meter = document.createElement("meter");
        meter.min = 0;
        meter.max = 100;
        meter.value = numericValue;
        meter.setAttribute("aria-label", `${COMPONENT_LABELS[key] || humanize(key)} ${formatScore(numericValue)} 分`);
        item.appendChild(meter);
        list.appendChild(item);
      });

      details.append(summary, list);
      column.appendChild(details);
    }

    return column;
  }

  function updateFilterCounts(picks) {
    const counts = { all: picks.length };
    Object.keys(REASONS).forEach((key) => {
      counts[key] = 0;
    });
    picks.forEach((pick) => {
      const reason = cleanText(pick && pick.selection_reason) || "best_available";
      counts[reason] = (counts[reason] || 0) + 1;
    });

    document.querySelectorAll("[data-count-for]").forEach((node) => {
      node.textContent = formatNumber(counts[node.dataset.countFor] || 0);
    });
  }

  function handleFilterClick(event) {
    const button = event.target.closest("button[data-filter]");
    if (!button || !dom.filterBar.contains(button)) return;
    applyFilter(button.dataset.filter);
  }

  function applyFilter(filter) {
    const normalized = filter === "all" || Object.hasOwn(REASONS, filter) ? filter : "all";
    state.activeFilter = normalized;
    updateActiveFilterButton();

    let visible = 0;
    dom.opportunityList.querySelectorAll(".opportunity-card").forEach((card) => {
      const shouldShow = normalized === "all" || card.dataset.reason === normalized;
      card.hidden = !shouldShow;
      if (shouldShow) visible += 1;
    });

    dom.filterEmpty.hidden = visible > 0 || state.picks.length === 0;
    dom.resultCount.textContent = normalized === "all"
      ? `共 ${formatNumber(visible)} 个精选机会`
      : `${REASONS[normalized].label} · ${formatNumber(visible)} 个`;
  }

  function updateActiveFilterButton() {
    dom.filterBar.querySelectorAll("button[data-filter]").forEach((button) => {
      const active = button.dataset.filter === state.activeFilter;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
  }

  async function runScan() {
    if (state.scanning) return;
    state.scanning = true;
    dom.scanButton.disabled = true;
    dom.scanButton.classList.add("is-scanning");
    dom.scanButton.setAttribute("aria-busy", "true");
    dom.scanLabel.textContent = "正在扫描 GitHub…";
    dom.scanNote.textContent = "正在检索并评估候选机会，请稍候";
    showToast("扫描已开始，正在更新候选池…");

    try {
      const scan = await requestJSON(API.scans, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });

      if (["failed", "error"].includes(String(scan.status || "").toLowerCase())) {
        throw new Error(scan.error_message || "扫描未能完成");
      }

      const summary = [
        Number.isFinite(Number(scan.candidate_count))
          ? `扫描 ${formatNumber(Number(scan.candidate_count))} 个候选`
          : "扫描完成",
        Number.isFinite(Number(scan.selected_count))
          ? `精选 ${formatNumber(Number(scan.selected_count))} 个机会`
          : "",
      ].filter(Boolean).join("，");

      showToast(`${summary}。榜单已更新。`);
      await reloadDaily();
    } catch (error) {
      showToast(`扫描失败：${friendlyError(error)}`, true);
    } finally {
      state.scanning = false;
      dom.scanButton.disabled = false;
      dom.scanButton.classList.remove("is-scanning");
      dom.scanButton.removeAttribute("aria-busy");
      dom.scanLabel.textContent = "立即扫描新机会";
      dom.scanNote.textContent = "扫描将更新今天的候选池与推荐榜单";
    }
  }

  async function reloadDaily() {
    try {
      const data = await requestJSON(API.daily);
      renderDaily(data);
    } catch (error) {
      showToast(`榜单刷新失败：${friendlyError(error)}`, true);
    }
  }

  function setLeaderboardLoading() {
    dom.opportunityList.replaceChildren();
    dom.filterEmpty.hidden = true;
    dom.resultCount.textContent = "载入中";

    const loading = element("div", "loading-state");
    for (let index = 0; index < 3; index += 1) {
      const skeleton = element("div", "skeleton-card");
      skeleton.setAttribute("aria-hidden", "true");
      loading.appendChild(skeleton);
    }
    loading.appendChild(element("span", "sr-only", "正在载入今日机会榜"));
    dom.statePanel.replaceChildren(loading);
  }

  function renderLeaderboardError(error) {
    dom.opportunityList.replaceChildren();
    dom.filterEmpty.hidden = true;
    dom.resultCount.textContent = "载入失败";

    const panel = element("div", "message-state is-error");
    const inner = element("div");
    inner.append(
      element("span", "message-symbol", "!"),
      element("strong", "", "暂时无法载入机会榜"),
      element("p", "", friendlyError(error)),
    );
    const retry = element("button", "secondary-button", "重新载入");
    retry.type = "button";
    retry.addEventListener("click", loadInitialData, { once: true });
    inner.appendChild(retry);
    panel.appendChild(inner);
    dom.statePanel.replaceChildren(panel);
  }

  function renderEmptyState() {
    const panel = element("div", "message-state");
    const inner = element("div");
    inner.append(
      element("span", "message-symbol", "⌁"),
      element("strong", "", "今天还没有可展示的机会"),
      element("p", "", "候选池可能尚未扫描，或暂时没有议题通过硬筛选。"),
    );
    const scan = element("button", "secondary-button", "发起首次扫描");
    scan.type = "button";
    scan.addEventListener("click", runScan);
    inner.appendChild(scan);
    panel.appendChild(inner);
    dom.statePanel.replaceChildren(panel);
  }

  function showToast(message, isError) {
    if (state.toastTimer) window.clearTimeout(state.toastTimer);
    dom.toast.textContent = message;
    dom.toast.classList.toggle("is-error", Boolean(isError));
    dom.toast.hidden = false;
    state.toastTimer = window.setTimeout(() => {
      dom.toast.hidden = true;
    }, isError ? 7000 : 4500);
  }

  function element(tagName, className, text) {
    const node = document.createElement(tagName);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function link(url, text, className) {
    const safeUrl = httpUrl(url);
    if (!safeUrl) {
      return element("span", className, text);
    }

    const anchor = element("a", className, text);
    anchor.href = safeUrl;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
    return anchor;
  }

  function httpUrl(value) {
    if (typeof value !== "string" || !value.trim()) return null;
    try {
      const url = new URL(value, window.location.origin);
      return ["http:", "https:"].includes(url.protocol) ? url.href : null;
    } catch (_error) {
      return null;
    }
  }

  function cleanText(value) {
    return typeof value === "string" ? value.trim() : "";
  }

  function toStringArray(value) {
    return Array.isArray(value)
      ? value.map(cleanText).filter(Boolean)
      : [];
  }

  function toFiniteNumber(value, fallback) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function toOptionalNumber(value) {
    if (value === null || value === undefined || value === "") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
  }

  function parseDate(value) {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function formatSelectionDate(value) {
    if (!value) return "等待数据";
    const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (match) return `${Number(match[2])} 月 ${Number(match[3])} 日`;
    const date = parseDate(value);
    return date
      ? new Intl.DateTimeFormat("zh-CN", { month: "long", day: "numeric" }).format(date)
      : String(value);
  }

  function formatNumber(value) {
    return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(value);
  }

  function formatCompact(value) {
    return new Intl.NumberFormat("zh-CN", {
      notation: "compact",
      maximumFractionDigits: 1,
    }).format(toFiniteNumber(value, 0));
  }

  function formatScore(value) {
    return Number(value).toLocaleString("zh-CN", {
      minimumFractionDigits: Number(value) % 1 ? 1 : 0,
      maximumFractionDigits: 1,
    });
  }

  function humanize(value) {
    return cleanText(value)
      .replace(/[_-]+/g, " ")
      .replace(/\b\w/g, (character) => character.toUpperCase()) || "其他";
  }

  function friendlyError(error) {
    if (error instanceof TypeError && /fetch/i.test(error.message)) {
      return "无法连接服务，请检查服务是否正在运行。";
    }
    return cleanText(error && error.message) || "发生未知错误，请稍后再试。";
  }
})();
