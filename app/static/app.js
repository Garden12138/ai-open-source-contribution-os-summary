import { mountContributionWorkbench } from "./workbench.js?v=workbench-v1";
import { requestArtifact, requestJSON, sleep } from "./api.js?v=analysis-context-v2";

(function () {
  "use strict";

  const API = {
    meta: "/api/v1/meta",
    daily: "/api/v1/opportunities/daily?limit=10",
    scans: "/api/v1/scans",
    jobs: "/api/v1/jobs",
    opportunities: "/api/v1/opportunities",
    tasks: "/api/v1/tasks",
    planVersions: "/api/v1/plan-versions",
    planApprovals: "/api/v1/plan-approvals",
    executions: "/api/v1/executions",
    reviews: "/api/v1/reviews",
    publishIntents: "/api/v1/publish-intents",
    draftPullRequests: "/api/v1/draft-pull-requests",
    dashboard: "/api/v1/contributions/dashboard",
    preferences: "/api/v1/preferences/current",
    recommendations: "/api/v1/recommendations",
    shortlist: "/api/v1/shortlist",
    compare: "/api/v1/opportunities/compare",
    notifications: "/api/v1/notifications",
    scanChanges: "/api/v1/scan-changes/latest",
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

  const EXECUTION_STAGE_LABELS = {
    explore: "Explore",
    implement: "Implement",
    verify: "Verify",
  };

  const EXECUTION_STATUS_LABELS = {
    pending: "等待调度",
    queued: "已排队",
    leased: "准备中",
    running: "运行中",
    succeeded: "成功",
    failed: "失败",
    cancelled: "已取消",
    timed_out: "已超时",
    passed: "通过",
    output_limit: "输出超限",
    not_run: "未运行",
  };

  const state = {
    picks: [],
    activeFilter: "all",
    toastTimer: null,
    scanning: false,
    activeJobId: null,
    csrfToken: null,
    accessTokenRequired: false,
    accessToken: null,
    retryJobId: null,
    cancelRequested: false,
    analysisReady: false,
    analysisPanels: new Map(),
    recommendations: [],
    recommendationFeed: {},
    preference: null,
    currentView: "discover",
    shortlist: [],
    compareSelection: new Set(),
    notifications: [],
    showDismissed: false,
    analysisFilter: "all",
    bulkAnalysisRunning: false,
    analysisProbePassed: false,
    analysisProvider: "none",
    analysisModel: "",
    implementationProvider: "none",
    implementationModel: "",
    reviewProvider: "none",
    reviewModel: "",
    sandboxStageRuntime: "none",
    draftPrPublisher: "fake",
    tasks: [],
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
      analysisMode: document.querySelector("#analysis-mode"),
      resultCount: document.querySelector("#result-count"),
      dismissedToggle: document.querySelector("#dismissed-toggle"),
      aiScreenButton: document.querySelector("#ai-screen-button"),
      aiProbeButton: document.querySelector("#ai-probe-button"),
      aiFilterButton: document.querySelector("#ai-filter-button"),
      aiScreenStatus: document.querySelector("#ai-screen-status"),
      filterBar: document.querySelector("#filter-bar"),
      statePanel: document.querySelector("#state-panel"),
      opportunityList: document.querySelector("#opportunity-list"),
      filterEmpty: document.querySelector("#filter-empty"),
      toast: document.querySelector("#toast"),
      contributionDashboard: document.querySelector("#contribution-dashboard"),
      funnelGrid: document.querySelector("#funnel-grid"),
      heatmapGrid: document.querySelector("#heatmap-grid"),
      taskHistory: document.querySelector("#task-history"),
      contributionWorkbench: document.querySelector("#contribution-workbench"),
      runtimeBoundary: document.querySelector("#runtime-boundary"),
      onboardingCard: document.querySelector("#onboarding-card"),
      preferenceForm: document.querySelector("#preference-form"),
      preferenceStatus: document.querySelector("#preference-status"),
      scanChanges: document.querySelector("#scan-changes"),
      shortlistCount: document.querySelector("#shortlist-count"),
      shortlistList: document.querySelector("#shortlist-list"),
      compareButton: document.querySelector("#compare-button"),
      comparisonGrid: document.querySelector("#comparison-grid"),
      notificationButton: document.querySelector("#notification-button"),
      preferenceButton: document.querySelector("#preference-button"),
      notificationCount: document.querySelector("#notification-count"),
      notificationDrawer: document.querySelector("#notification-drawer"),
      notificationList: document.querySelector("#notification-list"),
      notificationReadAll: document.querySelector("#notification-read-all"),
    });

    dom.scanButton.addEventListener("click", runScan);
    dom.filterBar.addEventListener("click", handleFilterClick);
    dom.dismissedToggle.addEventListener("click", toggleDismissedRecommendations);
    dom.aiScreenButton.addEventListener(
      "click",
      () => runRecommendationAnalysis(5),
    );
    dom.aiProbeButton.addEventListener("click", () => runRecommendationAnalysis(1));
    dom.aiFilterButton.addEventListener("click", toggleAnalysisFilter);
    dom.preferenceForm.addEventListener("submit", savePreference);
    dom.preferenceButton.addEventListener("click", togglePreferenceEditor);
    dom.compareButton.addEventListener("click", compareSelectedOpportunities);
    dom.notificationButton.addEventListener("click", toggleNotifications);
    dom.notificationReadAll.addEventListener("click", markAllNotificationsRead);
    window.addEventListener("hashchange", renderActiveView);

    renderActiveView();
    loadInitialData();
  }

  async function loadInitialData() {
    setLeaderboardLoading();

    const [
      metaResult,
      dailyResult,
      dashboardResult,
      preferenceResult,
      recommendationResult,
      shortlistResult,
      changesResult,
      notificationResult,
    ] = await Promise.allSettled([
      requestJSON(API.meta),
      requestJSON(API.daily),
      requestJSON(API.dashboard),
      requestJSON(API.preferences),
      requestJSON(recommendationEndpoint()),
      requestJSON(API.shortlist),
      requestJSON(API.scanChanges),
      requestJSON(API.notifications),
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

    if (dashboardResult.status === "fulfilled") {
      renderContributionDashboard(dashboardResult.value);
    }

    if (preferenceResult.status === "fulfilled") {
      renderPreference(preferenceResult.value);
    } else {
      dom.onboardingCard.hidden = false;
    }

    if (recommendationResult.status === "fulfilled") {
      renderRecommendations(recommendationResult.value);
    }

    if (shortlistResult.status === "fulfilled") {
      renderShortlist(shortlistResult.value);
    }

    if (changesResult.status === "fulfilled") {
      renderScanChanges(changesResult.value);
    }

    if (notificationResult.status === "fulfilled") {
      renderNotifications(notificationResult.value);
    }
  }

  function renderActiveView() {
    const requested = cleanText(window.location.hash).replace(/^#\//, "");
    const view = ["discover", "shortlist", "contributions"].includes(requested)
      ? requested
      : "discover";
    state.currentView = view;
    document.querySelectorAll("[data-product-view]").forEach((section) => {
      section.hidden = section.dataset.productView !== view;
    });
    document.querySelectorAll("[data-view-link]").forEach((link) => {
      const active = link.dataset.viewLink === view;
      link.classList.toggle("is-active", active);
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
  }

  function renderPreference(data) {
    const configured = Boolean(data && data.configured);
    const preference = objectValue(data && data.preference);
    state.preference = configured ? preference : null;
    dom.onboardingCard.hidden = configured;
    dom.preferenceButton.hidden = !configured;
    dom.preferenceButton.setAttribute("aria-expanded", "false");
    dom.preferenceButton.textContent = "调整偏好";
    if (!configured) return;
    const form = dom.preferenceForm.elements;
    form.primary_goal.value = cleanText(preference.primary_goal) || "balanced";
    form.preferred_languages.value = toStringArray(preference.preferred_languages).join(", ");
    form.weekly_hours.value = String(toFiniteNumber(preference.weekly_hours, 5));
    form.minimum_bounty_usd.value = String(toFiniteNumber(preference.minimum_bounty_usd, 0));
    form.auto_scan_enabled.checked = Boolean(preference.auto_scan_enabled);
    form.auto_scan_local_time.value = cleanText(preference.auto_scan_local_time) || "09:00";
  }

  async function savePreference(event) {
    event.preventDefault();
    const form = dom.preferenceForm.elements;
    const submit = dom.preferenceForm.querySelector("button[type='submit']");
    submit.disabled = true;
    dom.preferenceStatus.textContent = "正在保存并重新排序…";
    try {
      await ensureLocalAccessToken();
      const preference = await requestJSON(API.preferences, {
        method: "POST",
        headers: mutationHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          primary_goal: form.primary_goal.value,
          preferred_languages: form.preferred_languages.value
            .split(",")
            .map((item) => item.trim())
            .filter(Boolean),
          weekly_hours: Number(form.weekly_hours.value),
          minimum_bounty_usd: Number(form.minimum_bounty_usd.value),
          auto_scan_enabled: form.auto_scan_enabled.checked,
          auto_scan_local_time: form.auto_scan_local_time.value || "09:00",
        }),
      });
      state.preference = preference;
      dom.onboardingCard.hidden = true;
      dom.preferenceButton.hidden = false;
      dom.preferenceButton.setAttribute("aria-expanded", "false");
      dom.preferenceButton.textContent = "调整偏好";
      dom.preferenceStatus.textContent = "偏好已保存";
      await reloadProductExperience();
      showToast("推荐已按你的目标重新排序。 ");
    } catch (error) {
      dom.preferenceStatus.textContent = friendlyError(error);
    } finally {
      submit.disabled = false;
    }
  }

  function togglePreferenceEditor() {
    const opening = dom.onboardingCard.hidden;
    dom.onboardingCard.hidden = !opening;
    dom.preferenceButton.setAttribute("aria-expanded", String(opening));
    dom.preferenceButton.textContent = opening ? "收起偏好" : "调整偏好";
    if (opening) {
      dom.onboardingCard.scrollIntoView({ behavior: "smooth", block: "center" });
      dom.preferenceForm.elements.primary_goal.focus({ preventScroll: true });
    }
  }

  async function reloadProductExperience() {
    const [recommendations, shortlist, changes, notifications] = await Promise.all([
      requestJSON(recommendationEndpoint()),
      requestJSON(API.shortlist),
      requestJSON(API.scanChanges),
      requestJSON(API.notifications),
    ]);
    renderRecommendations(recommendations);
    renderShortlist(shortlist);
    renderScanChanges(changes);
    renderNotifications(notifications);
  }

  function recommendationEndpoint() {
    const query = new URLSearchParams();
    if (state.showDismissed) query.set("include_dismissed", "true");
    if (state.analysisFilter !== "all") {
      query.set("analysis_filter", state.analysisFilter);
    }
    const suffix = query.toString();
    return suffix ? `${API.recommendations}?${suffix}` : API.recommendations;
  }

  async function toggleAnalysisFilter() {
    const previous = state.analysisFilter;
    state.analysisFilter = previous === "recommended" ? "all" : "recommended";
    dom.aiFilterButton.disabled = true;
    dom.aiFilterButton.setAttribute(
      "aria-pressed",
      String(state.analysisFilter === "recommended"),
    );
    try {
      renderRecommendations(await requestJSON(recommendationEndpoint()));
    } catch (error) {
      state.analysisFilter = previous;
      showToast(friendlyError(error), true);
      dom.aiFilterButton.setAttribute(
        "aria-pressed",
        String(state.analysisFilter === "recommended"),
      );
    }
  }

  async function runRecommendationAnalysis(limit = 5) {
    if (state.bulkAnalysisRunning || !state.analysisReady) return;
    if (limit > 1 && !state.analysisProbePassed) return;
    state.bulkAnalysisRunning = true;
    dom.aiProbeButton.disabled = true;
    dom.aiScreenButton.disabled = true;
    dom.aiScreenButton.setAttribute("aria-busy", "true");
    dom.aiScreenButton.textContent = limit === 1 ? "正在验证…" : "正在排队…";
    dom.aiScreenStatus.textContent = limit === 1
      ? "正在验证 1 个真实 NVIDIA 分析样例；成功后才能运行前 5 个。"
      : "将为当前规则 Top 30 中最匹配的 5 个候选运行有预算上限的分析。";
    try {
      await ensureLocalAccessToken();
      const batch = await requestJSON(`${API.recommendations}/analyses`, {
        method: "POST",
        headers: mutationHeaders({
          "Content-Type": "application/json",
          "Idempotency-Key": randomKey("web-recommendation-analysis"),
        }),
        body: JSON.stringify({ limit }),
      });
      const entries = Array.isArray(batch.jobs) ? batch.jobs : [];
      if (!entries.length) {
        dom.aiScreenStatus.textContent = "当前可分析候选已有最新结论，或没有符合 Top 30 安全语料边界的候选。";
        showToast("当前候选无需新增 AI 分析。");
        return;
      }
      const batchJobs = new Map(
        entries.map((entry) => {
          const job = objectValue(entry.job);
          return [cleanText(job.id), job];
        }),
      );
      const renderBatchProgress = () => {
        const jobs = [...batchJobs.values()];
        const terminal = new Set(["succeeded", "failed", "cancelled", "timed_out"]);
        const completed = jobs.filter((job) => terminal.has(cleanText(job.state))).length;
        const running = jobs.filter((job) => ["leased", "running"].includes(cleanText(job.state))).length;
        const queued = jobs.filter((job) => cleanText(job.state) === "queued").length;
        dom.aiScreenButton.textContent = running
          ? `正在分析 ${completed}/${entries.length}`
          : `已排队 ${completed}/${entries.length}`;
        dom.aiScreenStatus.textContent = limit === 1
          ? `样例验证：已完成 ${completed}/1；正在执行 ${running} 个，排队等待 ${queued} 个。`
          : `AI 分析：已完成 ${completed}/${entries.length}；正在执行 ${running} 个，排队等待 ${queued} 个。`;
      };
      renderBatchProgress();
      const results = await Promise.all(entries.map(async (entry, index) => {
        const job = await waitForTerminalJob(objectValue(entry.job), {
          queuePosition: index,
          onUpdate: (updatedJob) => {
            batchJobs.set(cleanText(updatedJob.id), updatedJob);
            renderBatchProgress();
          },
        });
        return job;
      }));
      const succeeded = results.filter((job) => cleanText(job.state) === "succeeded").length;
      const failed = results.length - succeeded;
      if (limit === 1) {
        state.analysisProbePassed = succeeded === 1;
        dom.aiScreenStatus.textContent = succeeded === 1
          ? "样例验证通过；现在可以运行 AI 筛选前 5 个。"
          : "样例验证失败；不会启动前 5 个，请先检查 NVIDIA Gateway。";
        showToast(
          succeeded === 1 ? "样例验证通过。" : "样例验证失败，未启动批量分析。",
          failed > 0,
        );
      } else {
        dom.aiScreenStatus.textContent = failed
          ? `完成 ${succeeded} 个，${failed} 个失败；失败项保留规则评分。`
          : `已完成 ${succeeded} 个 AI 分析，并按结论更新决策排序。`;
        showToast(failed ? "部分 AI 分析未完成，已保留规则回退。" : "AI 筛选完成。", failed > 0);
      }
      await reloadProductExperience();
    } catch (error) {
      dom.aiScreenStatus.textContent = friendlyError(error);
      showToast(`AI 筛选失败：${friendlyError(error)}`, true);
    } finally {
      state.bulkAnalysisRunning = false;
      dom.aiScreenButton.removeAttribute("aria-busy");
      updateAnalysisScreenControls();
    }
  }

  async function toggleDismissedRecommendations() {
    state.showDismissed = !state.showDismissed;
    dom.dismissedToggle.disabled = true;
    dom.dismissedToggle.setAttribute("aria-pressed", String(state.showDismissed));
    dom.dismissedToggle.textContent = state.showDismissed
      ? "隐藏已忽略"
      : "查看已忽略";
    try {
      renderRecommendations(await requestJSON(recommendationEndpoint()));
    } catch (error) {
      state.showDismissed = !state.showDismissed;
      dom.dismissedToggle.setAttribute("aria-pressed", String(state.showDismissed));
      dom.dismissedToggle.textContent = state.showDismissed
        ? "隐藏已忽略"
        : "查看已忽略";
      showToast(friendlyError(error), true);
    } finally {
      dom.dismissedToggle.disabled = false;
    }
  }

  function renderRecommendations(data) {
    const items = Array.isArray(data && data.items) ? data.items : [];
    state.recommendations = items;
    state.recommendationFeed = objectValue(data);
    [...state.analysisPanels.keys()]
      .filter((key) => key.startsWith("discover:"))
      .forEach((key) => state.analysisPanels.delete(key));
    updateAnalysisScreenControls(data);
    if (!items.length) {
      state.picks = [];
      dom.opportunityList.replaceChildren();
      dom.statePanel.replaceChildren(
        analysisMessage(
          state.showDismissed ? "暂时没有可展示的机会" : "当前推荐已处理完",
          state.showDismissed
            ? "完成一次扫描后，新的匹配机会会出现在这里。"
            : "可以查看已忽略的机会，或扫描新的候选。",
        ),
      );
      dom.filterEmpty.hidden = true;
      updateFilterCounts([]);
      dom.resultCount.textContent = state.analysisFilter === "recommended"
        ? "0 个 AI 推荐机会"
        : "0 个匹配机会";
      return;
    }
    const picks = items.map((item, index) => recommendationAsPick(item, index));
    state.picks = picks;
    dom.opportunityList.replaceChildren();
    dom.statePanel.replaceChildren();
    const fragment = document.createDocumentFragment();
    picks.forEach((pick, index) => fragment.appendChild(buildOpportunityCard(pick, index)));
    dom.opportunityList.appendChild(fragment);
    updateFilterCounts(picks);
    applyFilter(state.activeFilter);
    dom.resultCount.textContent = `共 ${formatNumber(toFiniteNumber(data.total, items.length))} 个匹配机会`;
    const goalLabels = {
      balanced: "综合选择",
      bounty: "赚取赏金",
      impact: "提升影响力",
      quick_merge: "快速获得合并",
      learning: "技术成长",
    };
    const analyzed = toFiniteNumber(data.analyzed_total, 0);
    const recommended = toFiniteNumber(data.recommended_total, 0);
    const providerCopy = state.analysisProvider === "fake"
      ? "演示分析"
      : state.analysisReady
        ? "真实分析已就绪"
        : "规则回退";
    dom.analysisMode.textContent = `当前目标：${goalLabels[cleanText(data.goal)] || "综合选择"} · ${providerCopy} · ${formatNumber(analyzed)} 个已分析 / ${formatNumber(recommended)} 个 AI 推荐`;
  }

  function updateAnalysisScreenControls(data) {
    const feed = Object.keys(objectValue(data)).length
      ? objectValue(data)
      : state.recommendationFeed;
    const pending = toFiniteNumber(feed.pending_analysis_total, 0);
    const recommended = toFiniteNumber(feed.recommended_total, 0);
    dom.aiFilterButton.textContent = state.analysisFilter === "recommended"
      ? "显示全部候选"
      : `只看 AI 推荐 (${formatNumber(recommended)})`;
    dom.aiFilterButton.setAttribute(
      "aria-pressed",
      String(state.analysisFilter === "recommended"),
    );
    dom.aiFilterButton.disabled = state.bulkAnalysisRunning
      || (state.analysisFilter === "all" && recommended === 0);
    if (state.bulkAnalysisRunning) return;
    if (!state.analysisReady) {
      dom.aiScreenButton.textContent = "AI 筛选未配置";
      dom.aiScreenButton.disabled = true;
      dom.aiProbeButton.textContent = "AI 筛选未配置";
      dom.aiProbeButton.disabled = true;
      dom.aiScreenStatus.textContent = "当前查询结果使用硬筛选与规则/偏好排序；没有调用 LLM。";
      return;
    }
    dom.aiScreenButton.textContent = state.analysisProvider === "fake"
      ? "AI 筛选前 5 个（演示）"
      : "AI 筛选前 5 个";
    state.analysisProbePassed = state.analysisProbePassed
      || toFiniteNumber(feed.analyzed_total, 0) > 0;
    dom.aiProbeButton.textContent = state.analysisProvider === "fake"
      ? "先验证 1 个样例（演示）"
      : "先验证 1 个样例";
    dom.aiProbeButton.disabled = pending === 0;
    dom.aiScreenButton.disabled = pending === 0 || !state.analysisProbePassed;
    if (!dom.aiScreenStatus.textContent || pending === 0) {
      dom.aiScreenStatus.textContent = pending === 0
        ? "当前候选都已有与本轮 Snapshot 对应的分析。"
        : state.analysisProbePassed
          ? `${formatNumber(pending)} 个候选尚未分析；批量运行不会覆盖基础规则分。`
          : "请先验证 1 个真实 AI 分析样例；验证通过后才可运行前 5 个。";
    }
  }

  function recommendationAsPick(item, index) {
    const product = objectValue(item);
    const opportunity = objectValue(product.opportunity);
    const components = objectValue(opportunity.score_components);
    const firstReason = toStringArray(product.reason_codes)[0];
    const selectionReason = {
      reward_reliability: "bounty",
      project_impact: "high_impact",
      tech_match: "tech_match",
      learning_value: "strategic",
    }[firstReason] || "best_available";
    return {
      rank: index + 1,
      scan_run_id: product.scan_run_id,
      snapshot_id: product.snapshot_id,
      score_version_id: product.score_version_id,
      selection_reason: selectionReason,
      score_snapshot: product.decision_score ?? product.personalized_score,
      product_recommendation: product,
      opportunity: {
        ...opportunity,
        score_total: product.decision_score ?? product.personalized_score,
        score_components: components,
        is_tech_match: toStringArray(product.reason_codes).includes("tech_match"),
        is_strategic: toStringArray(product.reason_codes).includes("learning_value"),
      },
    };
  }

  function renderScanChanges(data) {
    const newMatches = toFiniteNumber(data && data.new_matches, 0);
    const updates = toFiniteNumber(data && data.shortlist_updates, 0);
    if (!data || !data.scan_run_id || (!newMatches && !updates)) {
      dom.scanChanges.hidden = true;
      return;
    }
    dom.scanChanges.hidden = false;
    dom.scanChanges.replaceChildren(
      element("strong", "", "自上次扫描后的变化"),
      element("span", "", `${formatNumber(newMatches)} 个新匹配 · ${formatNumber(updates)} 个候选有更新`),
    );
  }

  function renderNotifications(rows) {
    state.notifications = Array.isArray(rows) ? rows : [];
    const unread = state.notifications.filter((item) => !item.is_read);
    dom.notificationCount.textContent = formatNumber(unread.length);
    dom.notificationButton.classList.toggle("has-unread", unread.length > 0);
    dom.notificationList.replaceChildren();
    if (!state.notifications.length) {
      dom.notificationList.appendChild(element("p", "notification-empty", "暂时没有新提醒。"));
      return;
    }
    state.notifications.forEach((notification) => {
      const item = element("article", "notification-item");
      item.classList.toggle("is-read", Boolean(notification.is_read));
      item.append(
        element("strong", "", cleanText(notification.title)),
        element("p", "", cleanText(notification.message)),
        element("small", "", formatRelativeDate(notification.created_at)),
      );
      if (!notification.is_read) {
        const read = analysisAction("标记已读", async () => {
          await mutateJSON(`${API.notifications}/${encodeURIComponent(notification.id)}/read`, {});
          notification.is_read = true;
          renderNotifications(state.notifications);
        });
        read.classList.add("is-compact");
        item.appendChild(read);
      }
      dom.notificationList.appendChild(item);
    });
  }

  function toggleNotifications() {
    const opening = dom.notificationDrawer.hidden;
    dom.notificationDrawer.hidden = !opening;
    dom.notificationButton.setAttribute("aria-expanded", String(opening));
  }

  async function markAllNotificationsRead() {
    try {
      await mutateJSON(`${API.notifications}/read-all`, {});
      state.notifications.forEach((item) => { item.is_read = true; });
      renderNotifications(state.notifications);
    } catch (error) {
      showToast(friendlyError(error), true);
    }
  }

  async function mutateJSON(url, body) {
    await ensureLocalAccessToken();
    return requestJSON(url, {
      method: "POST",
      headers: mutationHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
    });
  }

  function renderShortlist(rows) {
    state.shortlist = Array.isArray(rows) ? rows : [];
    state.compareSelection.clear();
    dom.shortlistCount.textContent = formatNumber(state.shortlist.length);
    dom.compareButton.disabled = true;
    dom.shortlistList.replaceChildren();
    dom.comparisonGrid.hidden = true;
    if (!state.shortlist.length) {
      const empty = analysisMessage(
        "还没有候选机会",
        "在发现页把值得进一步考虑的机会加入候选，再回来比较。",
      );
      const discover = analysisAction("去发现机会", () => {
        window.location.hash = "#/discover";
      });
      empty.appendChild(discover);
      dom.shortlistList.appendChild(empty);
      return;
    }
    state.shortlist.forEach((item, index) => {
      dom.shortlistList.appendChild(buildShortlistCard(item, index));
    });
  }

  function buildShortlistCard(item, index) {
    const opportunity = objectValue(item.opportunity);
    const repository = objectValue(opportunity.repository);
    const card = element("article", "shortlist-card");
    const selectLabel = element("label", "compare-check");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.addEventListener("change", () => {
      if (checkbox.checked && state.compareSelection.size >= 3) {
        checkbox.checked = false;
        showToast("一次最多比较三个机会。", true);
        return;
      }
      if (checkbox.checked) state.compareSelection.add(opportunity.id);
      else state.compareSelection.delete(opportunity.id);
      dom.compareButton.disabled = state.compareSelection.size < 2;
      dom.compareButton.textContent = state.compareSelection.size >= 2
        ? `比较 ${state.compareSelection.size} 个机会`
        : "比较所选机会";
    });
    selectLabel.append(checkbox, document.createTextNode("选择比较"));

    const main = element("div", "shortlist-main");
    main.append(
      element("span", "shortlist-repo", cleanText(repository.full_name)),
      element("h3", "", cleanText(opportunity.title)),
      element("p", "", cleanText(item.summary)),
    );
    const facts = element("div", "shortlist-facts");
    facts.append(
      decisionMetric(
        cleanText(item.analysis_status) === "analyzed" ? "AI 结论" : "推荐",
        cleanText(item.analysis_status) === "analyzed"
          ? analysisRecommendationLabel(item.analysis_recommendation)
          : recommendationLabel(item.recommendation_label),
      ),
      decisionMetric("接收机会", levelLabel(item.acceptance_level)),
      decisionMetric("竞争压力", inverseLevelLabel(item.competition_level)),
      decisionMetric("预计投入", effortLabel(item.effort)),
    );
    main.appendChild(facts);

    const controls = element("div", "shortlist-controls");
    const workbench = buildAnalysisWorkbench(
      recommendationAsPick(item, index),
      opportunity,
      index,
      "shortlist",
    );
    workbench.classList.add("shortlist-analysis-workbench");
    const panel = state.analysisPanels.get(
      analysisPanelKey("shortlist", opportunity.id),
    );
    const startContribution = analysisAction(
      item.analysis_version_id ? "开始 Vibe Coding" : "先做 AI 评估",
      async () => {
        if (!panel) return;
        if (panel.body.hidden) await toggleAnalysisPanel(panel);
        workbench.scrollIntoView({ behavior: "smooth", block: "start" });
      },
    );
    startContribution.classList.add("shortlist-start-action");
    if (!item.analysis_version_id && !state.analysisReady) {
      startContribution.disabled = true;
      startContribution.title = "当前未配置分析服务，无法创建贡献任务";
    }
    const reminder = document.createElement("input");
    reminder.type = "datetime-local";
    reminder.setAttribute("aria-label", "提醒时间");
    const existingReminder = parseDate(item.reminder_at);
    if (existingReminder) reminder.value = toLocalInputValue(existingReminder);
    const saveReminder = analysisAction("保存提醒", async () => {
      try {
        await setOpportunityDisposition(opportunity.id, {
          state: "shortlisted",
          reason_code: null,
          reminder_at: reminder.value ? new Date(reminder.value).toISOString() : null,
        });
        showToast(reminder.value ? "提醒时间已保存。" : "提醒已清除。 ");
        await reloadProductExperience();
      } catch (error) {
        showToast(friendlyError(error), true);
      }
    });
    saveReminder.classList.add("is-compact");
    const remove = analysisAction("移出候选", async () => {
      await setOpportunityDisposition(opportunity.id, {
        state: "neutral",
        reason_code: null,
        reminder_at: null,
      });
      await reloadProductExperience();
    });
    remove.classList.add("is-compact", "is-danger");
    const issue = link(opportunity.html_url, "查看 Issue ↗", "open-issue-link");
    controls.append(startContribution, reminder, saveReminder, remove, issue);
    card.append(selectLabel, main, controls, workbench);
    return card;
  }

  async function compareSelectedOpportunities() {
    const query = new URLSearchParams();
    [...state.compareSelection].forEach((id) => query.append("opportunity_id", String(id)));
    dom.compareButton.disabled = true;
    dom.comparisonGrid.hidden = false;
    dom.comparisonGrid.replaceChildren(
      analysisMessage("正在比较", "汇总投入、收益、竞争和风险…", true),
    );
    try {
      const rows = await requestJSON(`${API.compare}?${query.toString()}`);
      renderComparison(rows);
    } catch (error) {
      dom.comparisonGrid.replaceChildren(
        analysisMessage("比较失败", friendlyError(error)),
      );
    } finally {
      dom.compareButton.disabled = state.compareSelection.size < 2;
    }
  }

  function renderComparison(rows) {
    dom.comparisonGrid.replaceChildren();
    (Array.isArray(rows) ? rows : []).forEach((item) => {
      const opportunity = objectValue(item.opportunity);
      const repository = objectValue(opportunity.repository);
      const card = element("article", "comparison-card");
      card.append(
        element("span", "shortlist-repo", cleanText(repository.full_name)),
        element("h3", "", cleanText(opportunity.title)),
        element(
          "strong",
          "comparison-verdict",
          cleanText(item.analysis_status) === "analyzed"
            ? analysisRecommendationLabel(item.analysis_recommendation)
            : recommendationLabel(item.recommendation_label),
        ),
        comparisonRow("决策排序", formatScore(item.decision_score)),
        comparisonRow("基础匹配", formatScore(item.personalized_score)),
        comparisonRow("预计投入", effortLabel(item.effort)),
        comparisonRow("接收机会", levelLabel(item.acceptance_level)),
        comparisonRow("竞争压力", inverseLevelLabel(item.competition_level)),
        comparisonRow("项目影响", levelLabel(item.impact_level)),
        comparisonRow(
          "赏金",
          opportunity.has_bounty
            ? opportunity.bounty_amount_usd == null
              ? "金额待确认"
              : formatCurrency(opportunity.bounty_amount_usd)
            : "无明确赏金",
        ),
      );
      const risks = toStringArray(opportunity.risk_reasons);
      card.appendChild(
        element(
          "p",
          "comparison-risk",
          risks.length
            ? risks.map((risk) => RISK_LABELS[risk] || humanize(risk)).join("；")
            : "暂无明显规则风险",
        ),
      );
      dom.comparisonGrid.appendChild(card);
    });
  }

  function comparisonRow(label, value) {
    const row = element("div", "comparison-row");
    row.append(element("span", "", label), element("strong", "", value));
    return row;
  }

  function recommendationLabel(value) {
    return {
      strong: "强推荐",
      worth_reviewing: "值得评估",
      cautious: "谨慎投入",
      low_priority: "优先级较低",
    }[cleanText(value)] || "待评估";
  }

  function analysisRecommendationLabel(value) {
    return {
      pursue: "建议投入",
      consider: "进一步确认",
      skip: "建议跳过",
      insufficient_evidence: "证据不足",
    }[cleanText(value)] || "已分析";
  }

  function effortLabel(value) {
    const effort = objectValue(value);
    const minimum = toOptionalNumber(effort.hours_min);
    const maximum = toOptionalNumber(effort.hours_max);
    return minimum !== null && maximum !== null
      ? `${formatNumber(minimum)}～${formatNumber(maximum)} 小时`
      : "待深入评估";
  }

  function renderMeta(meta) {
    const appName = cleanText(meta.app_name) || "开源机会雷达";
    const tokenConfigured = Boolean(meta.token_configured);
    state.csrfToken = cleanText(meta.csrf_token);
    state.accessTokenRequired = Boolean(meta.local_access_token_required);
    state.analysisProvider = cleanText(meta.analysis_provider) || "none";
    state.analysisModel = cleanText(meta.analysis_model);
    state.implementationProvider = cleanText(meta.implementation_provider) || "none";
    state.implementationModel = cleanText(meta.implementation_model);
    state.reviewProvider = cleanText(meta.review_provider) || "none";
    state.reviewModel = cleanText(meta.review_model);
    state.sandboxStageRuntime = cleanText(meta.sandbox_stage_runtime) || "none";
    state.draftPrPublisher = cleanText(meta.draft_pr_publisher) || "fake";
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
    summaries.push(state.analysisProvider === "nvidia_nim"
      ? "AI：真实分析已接入"
      : state.analysisProvider === "fake" ? "AI：Fake 演示" : "AI：未接入");
    dom.metaSummary.textContent = summaries.join(" · ");
    renderRuntimeBoundary();
  }

  function renderRuntimeBoundary() {
    if (!dom.runtimeBoundary) return;
    const analysis = state.analysisProvider === "nvidia_nim"
      ? "AI 分析使用真实模型"
      : state.analysisProvider === "fake"
        ? "AI 分析为 Fake 演示，不调用真实 LLM"
        : "真实 LLM 尚未接线";
    const execution = (
      state.implementationProvider === "nvidia_nim"
      && state.sandboxStageRuntime === "docker"
    )
      ? `Vibe Coding 使用 NVIDIA ${state.implementationModel} 与独立 Docker Sandbox Worker`
      : state.sandboxStageRuntime === "fake"
        ? "Vibe Coding 为 Fake Explore / Implement / Verify 演示"
        : "真实隔离代码执行尚未接线";
    const review = state.reviewProvider === "nvidia_nim"
      ? `独立审查使用 NVIDIA ${state.reviewModel}`
      : state.reviewProvider === "fake"
        ? "独立审查为 Fake 演示"
        : "真实独立审查尚未接线";
    const publish = state.draftPrPublisher === "fake"
      ? "Draft PR 仅写本地 Fake 记录，不会写 GitHub"
      : "Draft PR 发布能力未就绪";
    dom.runtimeBoundary.textContent = `${analysis}；${execution}；${review}；${publish}。`;
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
    state.analysisPanels.clear();
    const provenanceStatus = cleanText(data.provenance_status);
    if (provenanceStatus === "legacy_unverified") {
      dom.scanNote.textContent = "当前历史榜单缺少可验证的扫描关联";
    }
    renderAnalysisAvailability(data.analysis);

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

  function renderAnalysisAvailability(analysis) {
    const mode = cleanText(analysis && analysis.mode);
    const reasons = toStringArray(analysis && analysis.fallback_reasons);
    if (mode === "provider_ready") {
      state.analysisReady = true;
      dom.analysisMode.textContent = state.analysisProvider === "fake"
        ? "Fake 分析演示已就绪；不会调用真实 LLM，基础规则评分保持不变。"
        : "AI 深度分析运行条件已就绪；基础规则评分保持不变。";
      return;
    }

    state.analysisReady = false;
    const missing = [];
    if (reasons.includes("provider_not_configured")) missing.push("分析服务");
    if (reasons.includes("budget_not_configured")) missing.push("使用额度");
    const suffix = missing.length ? `（缺少：${missing.join("、")}）` : "";
    dom.analysisMode.textContent =
      `当前使用基础评分，深入评估仅在你主动发起时运行${suffix}。`;
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
    const product = objectValue(pick && pick.product_recommendation);
    const reason = cleanText(pick.selection_reason) || "best_available";
    const reasonMeta = REASONS[reason] || { label: humanize(reason), short: humanize(reason) };
    const rank = Math.max(1, Math.trunc(toFiniteNumber(pick.rank, index + 1)));
    const score = clamp(
      toFiniteNumber(pick.score_snapshot, toFiniteNumber(opportunity.score_total, 0)),
      0,
      100,
    );

    const card = element("article", "opportunity-card");
    const dismissed = cleanText(product.disposition_state) === "dismissed";
    card.classList.toggle("is-dismissed", dismissed);
    card.dataset.reason = reason;
    card.setAttribute("aria-labelledby", `opportunity-title-${index}`);

    const rankColumn = element("div", "rank-column");
    const rankBadge = element("span", "rank-badge", String(rank));
    if (rank <= 3) rankBadge.classList.add("is-podium");
    rankBadge.setAttribute("aria-label", `第 ${rank} 名`);
    rankColumn.appendChild(rankBadge);

    const main = element("div", "opportunity-main");
    const topline = element("div", "card-topline");
    const reasonBadge = element(
      "span",
      "reason-badge",
      dismissed ? "已忽略" : reasonMeta.label,
    );
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

    if (Object.keys(product).length) {
      main.appendChild(buildDecisionSummary(product, opportunity));
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

    if (Object.keys(product).length) {
      main.appendChild(buildProductActions(product, opportunity, "discover"));
    }

    main.appendChild(buildCardFooter(opportunity, repository));

    const scoreColumn = buildScoreColumn(
      score,
      opportunity.score_components,
      Object.keys(product).length > 0
        ? cleanText(product.analysis_status) === "analyzed"
          ? "AI 决策排序 / 100"
          : "个性化匹配 / 100"
        : "综合评分 / 100",
    );

    card.append(
      rankColumn,
      main,
      scoreColumn,
      buildAnalysisWorkbench(pick, opportunity, index, "discover"),
    );
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

  function buildDecisionSummary(product, opportunity) {
    const section = element("section", "decision-summary");
    const label = {
      strong: "强推荐",
      worth_reviewing: "值得评估",
      cautious: "谨慎投入",
      low_priority: "优先级较低",
    }[cleanText(product.recommendation_label)] || "值得评估";
    const aiRecommendation = {
      pursue: "AI 建议投入",
      consider: "AI 建议进一步确认",
      skip: "AI 建议跳过",
      insufficient_evidence: "AI 认为证据不足",
    }[cleanText(product.analysis_recommendation)];
    const heading = element("div", "decision-heading");
    heading.append(
      element("strong", "decision-verdict", aiRecommendation || label),
      element("p", "", cleanText(product.summary) || cleanText(opportunity.title)),
    );
    section.appendChild(heading);

    const reasons = element("ul", "decision-reasons");
    toStringArray(product.reasons).slice(0, 3).forEach((reason) => {
      reasons.appendChild(element("li", "", reason));
    });
    section.appendChild(reasons);

    const effort = objectValue(product.effort);
    const hoursMin = toOptionalNumber(effort.hours_min);
    const hoursMax = toOptionalNumber(effort.hours_max);
    const effortText = hoursMin !== null && hoursMax !== null
      ? `${formatNumber(hoursMin)}～${formatNumber(hoursMax)} 小时`
      : "待深入评估";
    const metrics = element("div", "decision-metrics");
    metrics.append(
      decisionMetric("预计投入", effortText),
      decisionMetric("接收机会", levelLabel(product.acceptance_level)),
      decisionMetric("竞争压力", inverseLevelLabel(product.competition_level)),
      decisionMetric("项目影响", levelLabel(product.impact_level)),
    );
    section.appendChild(metrics);

    if (opportunity.has_bounty) {
      const amount = toOptionalNumber(opportunity.bounty_amount_usd);
      section.appendChild(
        element(
          "p",
          "bounty-caveat",
          amount === null
            ? "可能存在赏金，金额和领取条款需要确认。"
            : `${formatCurrency(amount)} 已识别，领取条件和支付可靠性仍需确认。`,
        ),
      );
    }
    return section;
  }

  function decisionMetric(label, value) {
    const item = element("div", "decision-metric");
    item.append(element("span", "", label), element("strong", "", value));
    return item;
  }

  function buildProductActions(product, opportunity, scope) {
    const wrapper = element("div", "product-actions");
    const shortlisted = cleanText(product.disposition_state) === "shortlisted";
    const dismissed = cleanText(product.disposition_state) === "dismissed";
    const saveLabel = dismissed
      ? "恢复推荐"
      : shortlisted
        ? "已加入候选"
        : "加入候选";
    const save = analysisAction(saveLabel, async () => {
      save.disabled = true;
      try {
        await setOpportunityDisposition(opportunity.id, {
          state: shortlisted || dismissed ? "neutral" : "shortlisted",
          reason_code: null,
          reminder_at: null,
        });
        showToast(
          dismissed
            ? "已恢复到推荐列表。"
            : shortlisted
              ? "已移出候选。"
              : "已加入我的候选。 ",
        );
        await reloadProductExperience();
      } catch (error) {
        showToast(friendlyError(error), true);
      } finally {
        save.disabled = false;
      }
    });
    save.classList.add("product-primary-action");

    const analyze = analysisAction("深入评估", () => {
      const panel = state.analysisPanels.get(
        analysisPanelKey(scope, opportunity.id),
      );
      if (panel) toggleAnalysisPanel(panel);
    });

    const dismiss = analysisAction("暂不适合", () => {
      dismiss.hidden = true;
      reasonField.hidden = false;
      reasonField.querySelector("select").focus();
    });
    dismiss.hidden = dismissed;
    const reasonField = element("div", "dismiss-reason");
    reasonField.hidden = true;
    const select = document.createElement("select");
    [
      ["", "选择原因"],
      ["too_large", "任务过大"],
      ["low_reward", "收益偏低"],
      ["tech_mismatch", "技术不匹配"],
      ["high_competition", "竞争过高"],
      ["unclear_scope", "范围不清"],
      ["not_interested", "暂不感兴趣"],
      ["other", "其他"],
    ].forEach(([value, label]) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      select.appendChild(option);
    });
    const confirm = analysisAction("确认忽略", async () => {
      if (!select.value) return;
      confirm.disabled = true;
      try {
        await setOpportunityDisposition(opportunity.id, {
          state: "dismissed",
          reason_code: select.value,
          reminder_at: null,
        });
        showToast("已从推荐中隐藏；可通过“查看已忽略”恢复。 ");
        await reloadProductExperience();
      } catch (error) {
        showToast(friendlyError(error), true);
        confirm.disabled = false;
      }
    });
    confirm.classList.add("is-compact");
    reasonField.append(select, confirm);
    wrapper.append(save, analyze, dismiss, reasonField);
    return wrapper;
  }

  async function setOpportunityDisposition(opportunityId, payload) {
    return mutateJSON(
      `${API.opportunities}/${encodeURIComponent(opportunityId)}/dispositions`,
      payload,
    );
  }

  function levelLabel(value) {
    return { high: "高", medium: "中", low: "低" }[cleanText(value)] || "未知";
  }

  function inverseLevelLabel(value) {
    return { high: "低", medium: "中", low: "高" }[cleanText(value)] || "未知";
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

  function buildScoreColumn(score, components, caption) {
    const column = element("div", "score-column");
    const ring = element("div", "score-ring");
    ring.style.setProperty("--score", score.toFixed(1));
    ring.setAttribute("role", "img");
    ring.setAttribute("aria-label", `综合评分 ${formatScore(score)} 分`);
    ring.appendChild(element("span", "score-number", formatScore(score)));

    column.append(
      ring,
      element(
        "span",
        "score-caption",
        caption || "综合评分 / 100",
      ),
    );

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

  function analysisPanelKey(scope, opportunityId) {
    return `${cleanText(scope) || "discover"}:${Math.trunc(toFiniteNumber(opportunityId, 0))}`;
  }

  function buildAnalysisWorkbench(pick, opportunity, index, scope) {
    const opportunityId = Math.trunc(toFiniteNumber(opportunity.id, 0));
    const snapshotId = cleanText(pick && pick.snapshot_id);
    const section = element("section", "analysis-workbench");
    const header = element("div", "analysis-workbench-header");
    const heading = element("div");
    heading.append(
      element("strong", "", "深入评估"),
      element("span", "", "判断投入、工作量、风险和下一步"),
    );
    const toggle = element("button", "analysis-toggle", "查看分析报告");
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", "false");
    const panelId = `analysis-panel-${cleanText(scope) || "discover"}-${cleanText(index)}`;
    toggle.setAttribute("aria-controls", panelId);
    const product = objectValue(pick && pick.product_recommendation);
    const status = element(
      "span",
      "analysis-status",
      cleanText(product.analysis_status) === "analyzed" ? "已有当前分析" : "尚未载入",
    );
    if (cleanText(product.analysis_status) === "analyzed") {
      status.dataset.tone = "ready";
    }
    status.setAttribute("aria-live", "polite");
    const body = element("div", "analysis-panel");
    body.id = panelId;
    body.hidden = true;

    const panel = {
      opportunityId,
      snapshotId,
      section,
      toggle,
      status,
      body,
      loaded: false,
      loadToken: 0,
      versions: [],
      activeJobId: null,
      retryJobId: null,
      taskIds: new Map(),
    };
    if (opportunityId > 0) {
      state.analysisPanels.set(analysisPanelKey(scope, opportunityId), panel);
      toggle.addEventListener("click", () => toggleAnalysisPanel(panel));
    } else {
      toggle.disabled = true;
      status.textContent = "机会标识无效";
    }

    header.append(heading, status, toggle);
    section.append(header, body);
    return section;
  }

  async function toggleAnalysisPanel(panel) {
    const opening = panel.body.hidden;
    panel.body.hidden = !opening;
    panel.toggle.setAttribute("aria-expanded", String(opening));
    panel.toggle.textContent = opening ? "收起分析报告" : "查看分析报告";
    if (opening && !panel.loaded) await loadAnalysisHistory(panel);
  }

  function analysisEndpoint(panel, suffix) {
    const base = `${API.opportunities}/${encodeURIComponent(panel.opportunityId)}/analyses`;
    return suffix ? `${base}/${suffix}` : base;
  }

  async function loadAnalysisHistory(panel, force) {
    if (panel.loaded && !force) return;
    const token = panel.loadToken + 1;
    panel.loadToken = token;
    setAnalysisStatus(panel, "正在读取版本…", "loading");
    renderAnalysisLoading(panel, "正在载入分析报告历史");
    try {
      const history = await requestJSON(analysisEndpoint(panel));
      if (panel.loadToken !== token) return;
      panel.loaded = true;
      const versions = Array.isArray(history.versions) ? history.versions : [];
      panel.versions = versions.slice().sort((left, right) => {
        const leftCurrent = cleanText(left.snapshot_id) === panel.snapshotId ? 0 : 1;
        const rightCurrent = cleanText(right.snapshot_id) === panel.snapshotId ? 0 : 1;
        return leftCurrent - rightCurrent;
      });
      const currentCount = panel.versions.filter(
        (version) => cleanText(version.snapshot_id) === panel.snapshotId,
      ).length;
      setAnalysisStatus(
        panel,
        panel.versions.length
          ? `${currentCount} 个当前 / ${panel.versions.length} 个历史`
          : "暂无版本",
        currentCount ? "ready" : "empty",
      );
      renderAnalysisHistory(panel);
    } catch (error) {
      if (panel.loadToken !== token) return;
      panel.loaded = false;
      setAnalysisStatus(panel, "载入失败", "error");
      renderAnalysisError(panel, "无法载入分析历史", friendlyError(error), {
        retryHistory: true,
      });
    }
  }

  function renderAnalysisHistory(panel) {
    panel.body.replaceChildren();
    if (!panel.versions.length) {
      const copy = state.analysisReady
        ? "这个机会还没有深入评估。运行后会保留精简报告和版本记录。"
        : "当前仅提供基础评分；深入评估服务或使用额度尚未就绪。";
      const empty = analysisMessage("尚无深度分析", copy);
      if (state.analysisReady && panel.snapshotId) {
        empty.appendChild(analysisAction("运行深度分析", () => runAnalysis(panel)));
      }
      panel.body.appendChild(empty);
      return;
    }

    const toolbar = element("div", "analysis-toolbar");
    const versionGroup = element("label", "analysis-field");
    versionGroup.appendChild(element("span", "", "查看版本"));
    const versionSelect = document.createElement("select");
    panel.versions.forEach((version, index) => {
      const option = document.createElement("option");
      option.value = cleanText(version.id);
      option.textContent = analysisVersionLabel(version, index);
      versionSelect.appendChild(option);
    });
    versionGroup.appendChild(versionSelect);
    toolbar.appendChild(versionGroup);

    if (panel.versions.length > 1) {
      const compareGroup = element("label", "analysis-field");
      compareGroup.appendChild(element("span", "", "对比版本"));
      const compareSelect = document.createElement("select");
      panel.versions.slice(1).forEach((version, index) => {
        const option = document.createElement("option");
        option.value = cleanText(version.id);
        option.textContent = analysisVersionLabel(version, index + 1);
        compareSelect.appendChild(option);
      });
      compareGroup.appendChild(compareSelect);
      const compareButton = analysisAction("显示差异", () => {
        renderAnalysisComparison(
          panel,
          versionSelect.value,
          compareSelect.value,
        );
      });
      compareButton.classList.add("is-compact");
      toolbar.append(compareGroup, compareButton);
    }

    if (state.analysisReady && panel.snapshotId) {
      const rerun = analysisAction("生成新版本", () => runAnalysis(panel));
      rerun.classList.add("is-compact");
      toolbar.appendChild(rerun);
    } else {
      toolbar.appendChild(
        element("span", "analysis-fallback-badge", "基础评分 · 深入评估未开启"),
      );
    }

    const detailRoot = element("div", "analysis-detail-root");
    panel.body.append(toolbar, detailRoot);
    panel.detailRoot = detailRoot;
    versionSelect.addEventListener("change", () => {
      loadAnalysisDocument(panel, versionSelect.value);
    });
    loadAnalysisDocument(panel, versionSelect.value);
  }

  function analysisDocumentEndpoint(panel, versionId, download) {
    const suffix = `${encodeURIComponent(versionId)}/document`;
    return `${analysisEndpoint(panel, suffix)}${download ? "?download=true" : ""}`;
  }

  async function loadAnalysisDocument(panel, versionId) {
    if (!panel.detailRoot) return;
    panel.detailRoot.replaceChildren(
      analysisMessage("正在载入分析报告", "正在生成精简 Markdown 文档…", true),
    );
    try {
      const artifact = await requestArtifact(
        analysisDocumentEndpoint(panel, versionId, false),
      );
      const version = panel.versions.find(
        (item) => cleanText(item.id) === cleanText(versionId),
      ) || {};
      renderAnalysisDocument(panel.detailRoot, artifact.text, panel, version);
    } catch (error) {
      panel.detailRoot.replaceChildren(
        analysisMessage("分析报告载入失败", friendlyError(error)),
      );
    }
  }

  function renderAnalysisDocument(root, markdown, panel, version) {
    root.replaceChildren();
    const currentSnapshot = cleanText(version && version.snapshot_id) === panel.snapshotId;
    const enhancedAnalysis = cleanText(version && version.analyze_output_schema_version)
      === "analysis-schema-v4";

    if (!currentSnapshot) {
      const stale = element("aside", "planning-stale-alert");
      stale.append(
        element("strong", "", "历史 Snapshot 分析（只读）"),
        element("span", "", "这份结论不属于当前扫描 Snapshot，不能据此创建或继续贡献任务。"),
      );
      root.appendChild(stale);
    }
    if (!enhancedAnalysis) {
      const legacy = element("aside", "planning-stale-alert");
      legacy.append(
        element("strong", "", "旧版分析缺少项目—需求关联"),
        element("span", "", "这份历史结论未直接使用增强后的项目介绍和需求内容。"),
      );
      if (currentSnapshot && state.analysisReady && panel.snapshotId) {
        const rerun = analysisAction("重新生成关联分析", () => runAnalysis(panel));
        rerun.classList.add("is-compact");
        legacy.appendChild(rerun);
      }
      root.appendChild(legacy);
    }
    const documentRoot = element("article", "analysis-markdown-document");
    renderSafeMarkdown(documentRoot, markdown);
    root.appendChild(documentRoot);

    const documentActions = element("div", "analysis-document-actions");
    const download = element("a", "analysis-action is-secondary is-compact", "下载 Markdown");
    download.href = analysisDocumentEndpoint(panel, version.id, true);
    download.download = `analysis-${panel.opportunityId}.md`;
    documentActions.appendChild(download);
    root.appendChild(documentActions);
    if (currentSnapshot && enhancedAnalysis) {
      root.appendChild(buildPlanningLauncher(panel, version));
    }
  }

  function renderSafeMarkdown(root, markdown) {
    const lines = String(markdown || "").replace(/\r\n?/g, "\n").split("\n");
    let list = null;
    const finishList = () => {
      if (list) root.appendChild(list);
      list = null;
    };
    lines.forEach((line) => {
      const heading = /^(#{1,3}) (.+)$/.exec(line);
      const item = /^- (.*)$/.exec(line);
      if (heading) {
        finishList();
        const headingElement = element(`h${heading[1].length}`);
        appendMarkdownInline(headingElement, heading[2]);
        root.appendChild(headingElement);
      } else if (item) {
        if (!list) list = document.createElement("ul");
        const listItem = element("li");
        appendMarkdownInline(listItem, item[1]);
        list.appendChild(listItem);
      } else if (!line.trim()) {
        finishList();
      } else {
        finishList();
        const paragraph = element("p");
        appendMarkdownInline(paragraph, line);
        root.appendChild(paragraph);
      }
    });
    finishList();
  }

  function decodeMarkdownText(value) {
    return cleanText(value).replace(/\\([\\`*_{}\[\]<>#+\-.!|&])/g, "$1");
  }

  function appendMarkdownInline(parent, value) {
    const source = String(value || "");
    const markdownLink = /^(.*?)\[([^\]]+)\]\((https:\/\/github\.com\/[^\s)]+)\)(.*)$/.exec(source);
    if (!markdownLink) {
      parent.appendChild(document.createTextNode(decodeMarkdownText(source)));
      return;
    }
    parent.appendChild(document.createTextNode(decodeMarkdownText(markdownLink[1])));
    parent.appendChild(link(markdownLink[3], decodeMarkdownText(markdownLink[2])));
    parent.appendChild(document.createTextNode(decodeMarkdownText(markdownLink[4])));
  }

  async function renderAnalysisComparison(panel, leftVersionId, rightVersionId) {
    if (!panel.detailRoot || !leftVersionId || !rightVersionId) return;
    panel.detailRoot.replaceChildren(
      analysisMessage("正在计算版本差异", "比较只读取不可变 AnalysisVersion。", true),
    );
    const query = new URLSearchParams({
      left_version_id: leftVersionId,
      right_version_id: rightVersionId,
    });
    try {
      const comparison = await requestArtifact(
        `${analysisEndpoint(panel, "compare/document")}?${query.toString()}`,
      );
      const wrapper = element("section", "analysis-comparison analysis-markdown-document");
      renderSafeMarkdown(wrapper, comparison.text);
      const back = analysisAction("返回最新版本", () => renderAnalysisHistory(panel));
      back.classList.add("is-compact");
      wrapper.appendChild(back);
      panel.detailRoot.replaceChildren(wrapper);
    } catch (error) {
      panel.detailRoot.replaceChildren(
        analysisMessage("版本对比失败", friendlyError(error)),
      );
    }
  }

  function buildPlanningLauncher(panel, analysisDetail) {
    const section = element("section", "planning-launcher");
    const heading = element("div", "planning-launcher-heading");
    const copy = element("div");
    copy.append(
      element("span", "planning-kicker", "START CONTRIBUTING"),
      element("h4", "", "开始准备这个贡献"),
      element("p", "", "让 AI 阅读代码、讨论改造方案；您可以编辑，确认后自动实施和验证。"),
    );
    const body = element("div", "planning-launcher-body");
    const analysisVersionId = cleanText(analysisDetail && analysisDetail.id);
    const existingTask = state.tasks.find(
      (item) => cleanText(item.analysis_version_id) === analysisVersionId,
    );
    const knownTaskId = panel.taskIds.get(analysisVersionId)
      || cleanText(existingTask && existingTask.id);
    if (knownTaskId) panel.taskIds.set(analysisVersionId, knownTaskId);
    const action = analysisAction(
      knownTaskId ? "打开贡献方案" : "制定贡献方案",
      async () => {
        if (!analysisVersionId) {
          showToast("当前分析版本标识无效。", true);
          return;
        }
        action.disabled = true;
        action.textContent = knownTaskId ? "正在载入…" : "正在创建…";
        body.replaceChildren(
          analysisMessage("正在准备任务", "正在确认分析结论和机会信息。", true),
        );
        try {
          await ensureLocalAccessToken();
          const task = knownTaskId
            ? await requestJSON(`${API.tasks}/${encodeURIComponent(knownTaskId)}`)
            : await requestJSON(API.tasks, {
                method: "POST",
                headers: mutationHeaders({
                  "Content-Type": "application/json",
                  "Idempotency-Key": randomKey("web-task"),
                }),
                body: JSON.stringify({ analysis_version_id: analysisVersionId }),
              });
          const taskId = cleanText(task.task && task.task.id) || cleanText(task.id);
          if (!taskId) throw new Error("任务响应缺少标识");
          panel.taskIds.set(analysisVersionId, taskId);
          action.textContent = "任务已创建";
          await loadPlanningWorkbench(body, panel, analysisDetail, taskId);
        } catch (error) {
          body.replaceChildren(
            analysisMessage("任务工作台不可用", friendlyError(error)),
          );
          action.disabled = false;
          action.textContent = knownTaskId ? "重新打开" : "重试创建";
        }
      },
    );
    action.classList.add("planning-primary-action");
    heading.append(copy, action);
    section.append(heading, body);
    if (knownTaskId) action.click();
    return section;
  }

  async function loadPlanningWorkbench(container, panel, analysisDetail, taskId) {
    container.replaceChildren(
      analysisMessage("正在载入计划", "正在同步任务状态、计划和确认记录。", true),
    );
    try {
      const task = await requestJSON(`${API.tasks}/${encodeURIComponent(taskId)}`);
      renderPlanningWorkbench(container, panel, analysisDetail, task);
    } catch (error) {
      container.replaceChildren(
        analysisMessage("计划载入失败", friendlyError(error)),
      );
    }
  }

  function renderPlanningWorkbench(container, panel, analysisDetail, taskDetail) {
    mountContributionWorkbench(container, taskDetail.task.id, {
      ensureAccess: ensureLocalAccessToken,
      headers: mutationHeaders,
      renderExecution: (root, detail, plan, refresh) => renderExecutionDetail(root, detail, plan, refresh, true),
    });
  }

  function renderLegacyPlanningWorkbench(container, panel, analysisDetail, taskDetail) {
    container.replaceChildren();
    const task = objectValue(taskDetail.task);
    const currentState = objectValue(taskDetail.current_state);
    const plans = Array.isArray(taskDetail.plan_versions)
      ? taskDetail.plan_versions
      : [];
    const locks = Array.isArray(taskDetail.plan_locks)
      ? taskDetail.plan_locks
      : [];
    const approvals = Array.isArray(taskDetail.approvals)
      ? taskDetail.approvals
      : [];
    const conversation = Array.isArray(taskDetail.conversation)
      ? taskDetail.conversation
      : [];
    const executionIds = toStringArray(taskDetail.execution_attempt_ids);
    const latestExecutionId = cleanText(taskDetail.latest_execution_attempt_id)
      || (executionIds.length ? executionIds[executionIds.length - 1] : "");
    const latestPlan = plans.length ? plans[plans.length - 1] : null;
    const activeApproval = approvals.find(
      (item) => cleanText(item.id) === cleanText(taskDetail.active_approval_id),
    );
    const activeLock = activeApproval
      ? locks.find((item) => cleanText(item.id) === cleanText(activeApproval.plan_lock_id))
      : null;

    const statusBar = element("div", "planning-status-bar");
    statusBar.append(
      planningStatus("任务", "已创建", "ready"),
      planningStatus(
        "计划",
        latestPlan ? `v${latestPlan.version_number}` : "待创建",
        latestPlan ? "ready" : "empty",
      ),
      planningStatus(
        "批准",
        planningApprovalLabel(taskDetail.approval_status),
        cleanText(taskDetail.approval_status),
      ),
      planningStatus(
        "执行",
        latestExecutionId ? "已启动" : activeApproval ? "待校验" : "未就绪",
        latestExecutionId ? "ready" : activeApproval ? "pending" : "empty",
      ),
    );
    container.appendChild(statusBar);

    if (taskDetail.approval_status === "revoked") {
      const stale = element("div", "planning-stale-alert");
      stale.append(
        element("strong", "", "批准已失效"),
        element("span", "", "机会内容或计划依据已变化，请创建新修订并重新确认。"),
      );
      container.appendChild(stale);
    }

    const layout = element("div", "planning-layout");
    const main = element("div", "planning-main");
    const side = element("aside", "planning-side");
    if (latestExecutionId) {
      main.appendChild(
        buildExecutionWorkbench(latestExecutionId, latestPlan),
      );
    }
    main.appendChild(
      buildPlanEditor(
        task,
        latestPlan,
        currentState,
        analysisDetail,
        async () => loadPlanningWorkbench(container, panel, analysisDetail, task.id),
      ),
    );
    main.appendChild(
      buildPlanConversation(
        task,
        latestPlan,
        conversation,
        async () => loadPlanningWorkbench(container, panel, analysisDetail, task.id),
      ),
    );
    if (plans.length > 1) {
      main.appendChild(buildPlanDiff(task, plans));
    }
    side.appendChild(buildPlanVersionList(plans));
    side.appendChild(
      buildApprovalPanel(
        task,
        latestPlan,
        activeApproval,
        activeLock,
        taskDetail.approval_status,
        latestExecutionId,
        async () => loadPlanningWorkbench(container, panel, analysisDetail, task.id),
      ),
    );
    side.appendChild(buildPlanningProvenance(task, currentState, activeLock));
    side.appendChild(
      buildLifecyclePanel(
        task,
        currentState,
        async () => loadPlanningWorkbench(container, panel, analysisDetail, task.id),
      ),
    );
    layout.append(main, side);
    container.appendChild(layout);
  }

  function buildPlanEditor(task, latestPlan, currentState, analysisDetail, refresh) {
    const section = element("section", "planning-card");
    section.appendChild(
      planningCardHeading(
        latestPlan ? `计划修订 · 当前 v${latestPlan.version_number}` : "创建初始计划",
        "先确认目标、步骤和验收标准；高级执行设置可按需展开。",
      ),
    );
    const form = element("form", "plan-editor");
    const analysis = objectValue(
      objectValue(analysisDetail && analysisDetail.content).analysis,
    );
    const defaults = latestPlan || {
      goal: cleanText(analysis.problem_summary),
      acceptance_criteria: toStringArray(analysis.acceptance_criteria),
      files_to_inspect: [],
      files_likely_to_change: [],
      implementation_steps: [],
      tests_to_add_or_run: [],
      commands_to_run: [{
        command_id: "focused_tests",
        purpose: "运行聚焦测试",
        argv: ["python", "-m", "pytest", "-q"],
        working_directory: ".",
      }],
      risks: [],
      questions_for_maintainer: [],
    };
    form.append(
      planningTextField("目标", "goal", cleanText(defaults.goal), 3),
      planningTextField(
        "验收标准（每行一项）",
        "acceptance_criteria",
        toStringArray(defaults.acceptance_criteria).join("\n"),
        4,
      ),
    );
    const twoColumn = element("div", "planning-form-grid");
    twoColumn.append(
      planningTextField(
        "检查文件（每行一个仓库相对路径）",
        "files_to_inspect",
        toStringArray(defaults.files_to_inspect).join("\n"),
        4,
      ),
      planningTextField(
        "可能修改（每行一个仓库相对路径）",
        "files_likely_to_change",
        toStringArray(defaults.files_likely_to_change).join("\n"),
        4,
      ),
      planningTextField(
        "实施步骤（每行一项）",
        "implementation_steps",
        toStringArray(defaults.implementation_steps).join("\n"),
        5,
      ),
      planningTextField(
        "测试（每行一项）",
        "tests_to_add_or_run",
        toStringArray(defaults.tests_to_add_or_run).join("\n"),
        5,
      ),
      planningTextField(
        "风险（每行一项，可空）",
        "risks",
        toStringArray(defaults.risks).join("\n"),
        3,
      ),
      planningTextField(
        "给维护者的问题（每行一项，可空）",
        "questions_for_maintainer",
        toStringArray(defaults.questions_for_maintainer).join("\n"),
        3,
      ),
    );
    form.appendChild(twoColumn);
    const advancedCommands = document.createElement("details");
    advancedCommands.className = "technical-details";
    advancedCommands.append(
      element("summary", "", "高级执行设置"),
      planningTextField(
        "验证命令（高级）",
        "commands_to_run",
        JSON.stringify(defaults.commands_to_run || [], null, 2),
        9,
        "code",
      ),
    );
    form.appendChild(advancedCommands);
    const submit = analysisAction(
      latestPlan ? "保存为新修订" : "创建初始计划",
      () => {},
    );
    submit.type = "submit";
    const formStatus = element("span", "planning-form-status");
    form.append(submit, formStatus);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      submit.disabled = true;
      formStatus.textContent = "正在校验并保存…";
      try {
        await ensureLocalAccessToken();
        const payload = planFormPayload(form, latestPlan && latestPlan.id);
        await requestJSON(
          `${API.tasks}/${encodeURIComponent(task.id)}/plan-versions`,
          {
            method: "POST",
            headers: mutationHeaders({
              "Content-Type": "application/json",
              "Idempotency-Key": randomKey("web-plan"),
            }),
            body: JSON.stringify(payload),
          },
        );
        showToast(latestPlan ? "计划修订已保存。" : "初始计划已创建。");
        await refresh();
      } catch (error) {
        formStatus.textContent = friendlyError(error);
        submit.disabled = false;
      }
    });
    if (cleanText(currentState.to_state) !== "planning") {
      form.querySelectorAll("textarea, button").forEach((control) => {
        control.disabled = true;
      });
      formStatus.textContent = "已批准计划不可修改；输入失效后回到 planning 才能创建子修订。";
    }
    section.appendChild(form);
    return section;
  }

  function buildPlanConversation(task, latestPlan, entries, refresh) {
    const section = element("section", "planning-card");
    section.appendChild(
      planningCardHeading("计划对话与决策", "消息按任务形成不可变哈希链。"),
    );
    const list = element("div", "planning-conversation");
    if (!entries.length) {
      list.appendChild(element("p", "planning-empty-copy", "还没有计划讨论。"));
    } else {
      entries.forEach((entry) => {
        const item = element("article", "planning-message");
        const header = element("div", "");
        header.append(
          element("strong", "", cleanText(entry.actor_id) || "unknown"),
          element("span", "", `#${entry.sequence} · ${cleanText(entry.entry_type)}`),
        );
        item.append(
          header,
          element(
            "p",
            "",
            cleanText(objectValue(entry.content).text)
              || cleanText(objectValue(entry.content).rationale)
              || compactValue(entry.content),
          ),
        );
        list.appendChild(item);
      });
    }
    const form = element("form", "planning-chat-form");
    const actor = document.createElement("input");
    actor.name = "actor_id";
    actor.value = "local-user";
    actor.setAttribute("aria-label", "对话操作者");
    actor.maxLength = 128;
    const message = document.createElement("textarea");
    message.name = "message";
    message.rows = 3;
    message.maxLength = 8000;
    message.required = true;
    message.placeholder = "提出修改建议、澄清问题或记录计划讨论…";
    const send = analysisAction("追加消息", () => {});
    send.type = "submit";
    const status = element("span", "planning-form-status");
    form.append(actor, message, send, status);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      send.disabled = true;
      try {
        await ensureLocalAccessToken();
        await requestJSON(
          `${API.tasks}/${encodeURIComponent(task.id)}/conversation/messages`,
          {
            method: "POST",
            headers: mutationHeaders({
              "Content-Type": "application/json",
              "Idempotency-Key": randomKey("web-plan-message"),
            }),
            body: JSON.stringify({
              plan_version_id: latestPlan ? latestPlan.id : null,
              actor_id: actor.value.trim(),
              text: message.value.trim(),
            }),
          },
        );
        showToast("计划消息已追加。");
        await refresh();
      } catch (error) {
        status.textContent = friendlyError(error);
        send.disabled = false;
      }
    });
    section.append(list, form);
    return section;
  }

  function buildPlanDiff(task, plans) {
    const section = element("section", "planning-card");
    section.appendChild(
      planningCardHeading("计划版本差异", "语义字段差异与有界文本 diff。"),
    );
    const controls = element("div", "planning-diff-controls");
    const left = document.createElement("select");
    const right = document.createElement("select");
    plans.forEach((plan, index) => {
      const option = document.createElement("option");
      option.value = cleanText(plan.id);
      option.textContent = `v${plan.version_number}`;
      left.appendChild(option);
      right.appendChild(option.cloneNode(true));
      if (index === plans.length - 2) left.value = option.value;
      if (index === plans.length - 1) right.value = option.value;
    });
    const compare = analysisAction("显示差异", async () => {
      output.replaceChildren(analysisMessage("正在比较", "只读取不可变计划版本。", true));
      const query = new URLSearchParams({
        left_version_id: left.value,
        right_version_id: right.value,
      });
      try {
        const data = await requestJSON(
          `${API.tasks}/${encodeURIComponent(task.id)}/plan-versions/compare?${query}`,
        );
        renderPlanDiff(output, data);
      } catch (error) {
        output.replaceChildren(analysisMessage("比较失败", friendlyError(error)));
      }
    });
    const output = element("div", "planning-diff-output");
    controls.append(left, right, compare);
    section.append(controls, output);
    return section;
  }

  function renderPlanDiff(root, comparison) {
    root.replaceChildren();
    const differences = Array.isArray(comparison.semantic_differences)
      ? comparison.semantic_differences
      : [];
    if (!differences.length) {
      root.appendChild(element("p", "planning-empty-copy", "两个计划没有字段差异。"));
      return;
    }
    const list = element("div", "difference-list");
    differences.slice(0, 100).forEach((difference) => {
      const item = element("article", "difference-item");
      item.append(
        element("code", "", cleanText(difference.path) || "/"),
        analysisTextBlock("旧值", compactValue(difference.left)),
        analysisTextBlock("新值", compactValue(difference.right)),
      );
      list.appendChild(item);
    });
    const textDiff = element("pre", "planning-unified-diff");
    textDiff.textContent = cleanText(comparison.unified_diff);
    root.append(list, textDiff);
  }

  function buildPlanVersionList(plans) {
    const section = element("section", "planning-card planning-version-card");
    section.appendChild(
      planningCardHeading("版本链", `${plans.length} 个不可变版本`),
    );
    const list = element("ol", "planning-version-list");
    plans.forEach((plan) => {
      const item = document.createElement("li");
      item.append(
        element("strong", "", `v${plan.version_number}`),
        element("span", "", shortHash(plan.record_hash)),
      );
      list.appendChild(item);
    });
    if (!plans.length) {
      list.appendChild(element("li", "planning-empty-copy", "尚未创建计划"));
    }
    section.appendChild(list);
    return section;
  }

  function buildApprovalPanel(
    task,
    latestPlan,
    activeApproval,
    activeLock,
    approvalStatus,
    latestExecutionId,
    refresh,
  ) {
    const section = element("section", "planning-card planning-approval-card");
    section.appendChild(
      planningCardHeading("批准与执行就绪", "批准、启动执行、发布 Draft PR 是三个独立动作。"),
    );
    if (!latestPlan) {
      section.appendChild(element("p", "planning-empty-copy", "先创建计划，才能锁定和批准。"));
      return section;
    }
    if (latestExecutionId) {
      const started = element("div", "planning-approved-summary");
      started.append(
        element("strong", "", "隔离执行已启动"),
        element("span", "", `Execution ${shortHash(latestExecutionId)}`),
        element("span", "", "批准与执行记录均已冻结"),
      );
      section.appendChild(started);
      return section;
    }
    if (!activeApproval) {
      const form = element("form", "planning-approval-form");
      const base = document.createElement("input");
      base.name = "base_commit_sha";
      base.required = true;
      base.pattern = "(?:[0-9a-f]{40}|[0-9a-f]{64})";
      base.placeholder = "仓库 base commit SHA";
      base.setAttribute("aria-label", "仓库 base commit SHA");
      const actor = document.createElement("input");
      actor.name = "actor_id";
      actor.required = true;
      actor.value = "local-user";
      actor.setAttribute("aria-label", "批准操作者");
      const approve = analysisAction(
        approvalStatus === "revoked" ? "重新批准当前修订" : "批准此计划",
        () => {},
      );
      approve.type = "submit";
      const status = element("span", "planning-form-status");
      form.append(base, actor, approve, status);
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        approve.disabled = true;
        try {
          await ensureLocalAccessToken();
          await requestJSON(
            `${API.planVersions}/${encodeURIComponent(latestPlan.id)}/approve`,
            {
              method: "POST",
              headers: mutationHeaders({
                "Content-Type": "application/json",
                "Idempotency-Key": randomKey("web-approve-plan"),
              }),
              body: JSON.stringify({
                base_commit_sha: base.value.trim(),
                actor_id: actor.value.trim(),
              }),
            },
          );
          showToast("计划已按精确哈希批准。");
          await refresh();
        } catch (error) {
          status.textContent = friendlyError(error);
          approve.disabled = false;
        }
      });
      section.appendChild(form);
      return section;
    }

    const approved = element("div", "planning-approved-summary");
    approved.append(
      element("strong", "", "计划已批准"),
      element("span", "", `Approval ${shortHash(activeApproval.approval_hash)}`),
      element("span", "", `Base ${shortHash(activeLock && activeLock.base_commit_sha)}`),
    );
    const readinessForm = element("form", "planning-readiness-form");
    const observedBase = document.createElement("input");
    observedBase.required = true;
    observedBase.pattern = "(?:[0-9a-f]{40}|[0-9a-f]{64})";
    observedBase.value = cleanText(activeLock && activeLock.base_commit_sha);
    observedBase.setAttribute("aria-label", "当前观察到的 base commit SHA");
    const check = analysisAction("验证执行就绪", () => {});
    check.type = "submit";
    const result = element("div", "planning-readiness-result");
    readinessForm.append(observedBase, check);
    readinessForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      check.disabled = true;
      result.textContent = "正在校验完整批准指纹…";
      result.dataset.tone = "pending";
      try {
        await ensureLocalAccessToken();
        const readiness = await requestJSON(
          `${API.planApprovals}/${encodeURIComponent(activeApproval.id)}/execution-readiness`,
          {
            method: "POST",
            headers: mutationHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify({ base_commit_sha: observedBase.value.trim() }),
          },
        );
        result.textContent = readiness.ready && !readiness.execution_started
          ? "执行就绪已验证；尚未启动执行。"
          : "执行仍未就绪。";
        result.dataset.tone = readiness.ready ? "ready" : "error";
      } catch (error) {
        result.textContent = friendlyError(error);
        result.dataset.tone = "error";
        await refresh();
      } finally {
        check.disabled = false;
      }
    });
    const archiveForm = element("form", "planning-execution-form");
    const capture = analysisAction("采集仓库归档", () => {});
    capture.type = "submit";
    const captureStatus = element("span", "planning-form-status");
    archiveForm.append(capture, captureStatus);
    archiveForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      capture.disabled = true;
      captureStatus.textContent = "正在排队只读归档采集…";
      try {
        await ensureLocalAccessToken();
        const job = await waitForTerminalJob(
          await requestJSON(
            `${API.planVersions}/${encodeURIComponent(latestPlan.id)}/archives`,
            {
              method: "POST",
              headers: mutationHeaders({
                "Content-Type": "application/json",
                "Idempotency-Key": randomKey("web-archive"),
              }),
              body: JSON.stringify({
                approval_id: activeApproval.id,
                base_commit_sha: observedBase.value.trim(),
              }),
            },
          ),
        );
        const hash = job && job.result_data && job.result_data.archive_hash;
        if (
          String(job.state || "").toLowerCase() !== "succeeded"
          || !/^[0-9a-f]{64}$/.test(String(hash || ""))
        ) {
          throw new Error(job.error_message || "归档采集失败");
        }
        archiveHash.value = String(hash);
        captureStatus.textContent = `归档已固化：${String(hash).slice(0, 12)}…`;
        showToast("仓库归档已采集，可启动隔离执行。");
      } catch (error) {
        captureStatus.textContent = friendlyError(error);
      } finally {
        capture.disabled = false;
      }
    });
    const executionForm = element("form", "planning-execution-form");
    const archiveHash = document.createElement("input");
    archiveHash.name = "repository_archive_hash";
    archiveHash.required = true;
    archiveHash.pattern = "[0-9a-f]{64}";
    archiveHash.placeholder = "精确 repository archive SHA-256";
    archiveHash.setAttribute("aria-label", "repository archive SHA-256");
    const runnerDigest = document.createElement("input");
    runnerDigest.name = "runner_image_digest";
    runnerDigest.required = true;
    runnerDigest.pattern = "sha256:[0-9a-f]{64}";
    runnerDigest.placeholder = "Runner image digest（sha256:…）";
    runnerDigest.setAttribute("aria-label", "Runner image digest");
    const actor = document.createElement("input");
    actor.name = "actor_id";
    actor.required = true;
    actor.value = "local-user";
    actor.setAttribute("aria-label", "执行操作者");
    const start = analysisAction("启动隔离执行", () => {});
    start.type = "submit";
    const startStatus = element("span", "planning-form-status");
    executionForm.append(
      archiveHash,
      runnerDigest,
      actor,
      start,
      startStatus,
    );
    executionForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      start.disabled = true;
      startStatus.textContent = "正在复验批准指纹并创建执行…";
      try {
        await ensureLocalAccessToken();
        await requestJSON(
          `${API.planVersions}/${encodeURIComponent(latestPlan.id)}/executions`,
          {
            method: "POST",
            headers: mutationHeaders({
              "Content-Type": "application/json",
              "Idempotency-Key": randomKey("web-execution"),
            }),
            body: JSON.stringify({
              approval_id: activeApproval.id,
              base_commit_sha: observedBase.value.trim(),
              repository_archive_hash: archiveHash.value.trim(),
              runner_image_digest: runnerDigest.value.trim(),
              actor_id: actor.value.trim(),
            }),
          },
        );
        showToast("隔离执行已创建，Explore 阶段等待调度。");
        await refresh();
      } catch (error) {
        startStatus.textContent = friendlyError(error);
        start.disabled = false;
      }
    });
    const executionCopy = element(
      "p",
      "planning-empty-copy",
      "先采集只读归档（凭证留在编排层），再冻结归档哈希、Runner digest 与服务器沙箱策略；不会自动写入 GitHub。",
    );
    section.append(
      approved,
      readinessForm,
      result,
      executionCopy,
      archiveForm,
      executionForm,
    );
    return section;
  }

  function buildExecutionWorkbench(executionId, plan) {
    const section = element("section", "planning-card execution-workbench");
    const liveVibeCoding = (
      state.implementationProvider === "nvidia_nim"
      && state.sandboxStageRuntime === "docker"
    );
    const heading = planningCardHeading(
      liveVibeCoding
        ? "Vibe Coding · NVIDIA"
        : state.sandboxStageRuntime === "fake" ? "Vibe Coding（Fake 演示）" : "隔离执行",
      liveVibeCoding
        ? `在冻结的仓库上下文中与 ${state.implementationModel} 多轮协作；只有你确认的精确 ChangeSet 哈希会进入 Implement。`
        : state.sandboxStageRuntime === "fake"
        ? "当前不会真实修改仓库；页面演示 Explore、Implement、Verify 和产物审查。"
        : "关注执行进度和结果；保护策略与运行记录可按需展开。",
    );
    const refreshButton = analysisAction("刷新执行", () => load(true));
    refreshButton.classList.add("is-compact");
    heading.appendChild(refreshButton);
    const body = element("div", "execution-workbench-body");
    body.setAttribute("aria-live", "polite");
    section.append(heading, body);

    let loadSequence = 0;
    let refreshTimer = null;
    const load = async (manual) => {
      const sequence = loadSequence + 1;
      loadSequence = sequence;
      if (refreshTimer) {
        window.clearTimeout(refreshTimer);
        refreshTimer = null;
      }
      refreshButton.disabled = true;
      if (!manual || !body.childElementCount) {
        body.replaceChildren(
          analysisMessage("正在载入执行", "正在检查进度、变更和测试结果。", true),
        );
      }
      try {
        const detail = await requestJSON(
          `${API.executions}/${encodeURIComponent(executionId)}`,
        );
        if (sequence !== loadSequence || !section.isConnected) return;
        renderExecutionDetail(body, detail, plan, () => load(true));
        const runs = Array.isArray(detail.stage_runs) ? detail.stage_runs : [];
        if (runs.some((run) => !executionJobTerminal(objectValue(run.job).state))) {
          refreshTimer = window.setTimeout(() => {
            if (section.isConnected) load(false);
          }, 2000);
        }
      } catch (error) {
        if (sequence !== loadSequence || !section.isConnected) return;
        const failure = analysisMessage(
          "执行详情载入失败",
          friendlyError(error),
        );
        const retry = analysisAction("重试", () => load(true));
        retry.classList.add("is-compact");
        failure.appendChild(retry);
        body.replaceChildren(failure);
      } finally {
        if (sequence === loadSequence) refreshButton.disabled = false;
      }
    };
    load(false);
    return section;
  }

  function renderExecutionDetail(root, detail, plan, refresh, automated = false) {
    root.replaceChildren();
    const current = objectValue(detail.current_stage);
    const stages = Array.isArray(detail.stages) ? detail.stages : [];
    const runs = Array.isArray(detail.stage_runs) ? detail.stage_runs : [];
    const manifests = Array.isArray(detail.artifact_manifests)
      ? detail.artifact_manifests
      : [];
    const latestRun = runs.length ? runs[runs.length - 1] : null;
    const latestJob = objectValue(latestRun && latestRun.job);

    const summary = element("div", "execution-summary-grid");
    summary.append(
      analysisMetric("阶段", executionStageLabel(current.stage)),
      analysisMetric("状态", executionStatusLabel(current.status)),
      analysisMetric("Attempt", `#${toFiniteNumber(detail.attempt_number, 1)}`),
      analysisMetric("仓库", cleanText(detail.repository_full_name) || "—"),
    );
    root.appendChild(summary);

    if (
      !automated && cleanText(current.stage) === "explore"
      && cleanText(current.status) === "succeeded"
    ) {
      if (
        state.implementationProvider === "nvidia_nim"
        && state.sandboxStageRuntime === "docker"
      ) {
        root.appendChild(buildVibeCodingPanel(detail, refresh));
      } else {
      const changeForm = element("form", "planning-execution-form");
      const submitChange = analysisAction("提交变更方案", () => {});
      submitChange.type = "submit";
      const changeStatus = element("span", "planning-form-status");
      changeForm.append(submitChange, changeStatus);
      changeForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        submitChange.disabled = true;
        changeStatus.textContent = "正在绑定 ChangeSet 并排队 Implement…";
        try {
          await ensureLocalAccessToken();
          await requestJSON(
            `${API.executions}/${encodeURIComponent(detail.id)}/change-sets`,
            {
              method: "POST",
              headers: mutationHeaders({
                "Content-Type": "application/json",
                "Idempotency-Key": randomKey("web-changeset"),
              }),
              body: JSON.stringify({ source: "fake" }),
            },
          );
          showToast("ChangeSet 已接受，Implement 等待调度。");
          refresh();
        } catch (error) {
          changeStatus.textContent = friendlyError(error);
          submitChange.disabled = false;
        }
      });
      root.appendChild(changeForm);
      }
    }

    const reviews = Array.isArray(detail.reviews) ? detail.reviews : [];
    const latestReview = reviews.length ? reviews[reviews.length - 1] : null;
    if (
      !automated && cleanText(current.stage) === "verify"
      && cleanText(current.status) === "succeeded"
      && !latestReview
    ) {
      const reviewForm = element("form", "planning-execution-form");
      const startReview = analysisAction("启动独立 Review", () => {});
      startReview.type = "submit";
      const reviewStatus = element("span", "planning-form-status");
      reviewForm.append(startReview, reviewStatus);
      reviewForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        startReview.disabled = true;
        reviewStatus.textContent = "正在绑定 Verify 产物并启动独立 Review…";
        try {
          await ensureLocalAccessToken();
          const providerReview = state.reviewProvider === "nvidia_nim";
          const queued = await requestJSON(
            `${API.executions}/${encodeURIComponent(detail.id)}/${providerReview ? "review-jobs" : "reviews"}`,
            {
              method: "POST",
              headers: mutationHeaders({
                "Content-Type": "application/json",
                "Idempotency-Key": randomKey("web-review"),
              }),
              body: JSON.stringify(
                providerReview
                  ? { actor_id: "local-user" }
                  : { actor_id: "local-user", reviewer: "fake" },
              ),
            },
          );
          if (providerReview) {
            reviewStatus.textContent = `${state.reviewModel} 正在审查精确 diff 与测试产物…`;
            const completed = await waitForTerminalJob(queued);
            if (cleanText(completed.state) !== "succeeded") {
              throw new Error(completed.error_message || `${state.reviewModel} Review 失败`);
            }
          }
          showToast(providerReview ? `${state.reviewModel} Review 已完成。` : "独立 Review 已完成。");
          refresh();
        } catch (error) {
          reviewStatus.textContent = friendlyError(error);
          startReview.disabled = false;
        }
      });
      root.appendChild(reviewForm);
    }
    if (latestReview) {
      root.appendChild(buildReviewCard(latestReview, refresh, automated));
    }

    if (
      ["failed", "cancelled", "timed_out"].includes(cleanText(current.status))
      || ["failed", "cancelled", "timed_out"].includes(cleanText(latestJob.state))
    ) {
      const failure = element("div", "execution-alert");
      failure.dataset.tone = cleanText(current.status);
      failure.append(
        element(
          "strong",
          "",
          executionStatusLabel(current.status || latestJob.state),
        ),
        element(
          "span",
          "",
          cleanText(latestJob.error_message)
            || `Reason: ${cleanText(current.reason_code) || "unknown"}`,
        ),
      );
      root.appendChild(failure);
    }

    const timeline = element("ol", "execution-stage-timeline");
    ["explore", "implement", "verify"].forEach((stageName) => {
      const history = stages.filter(
        (item) => cleanText(item.stage) === stageName,
      );
      const latest = history.length ? history[history.length - 1] : null;
      const item = element("li", "execution-stage-step");
      item.dataset.status = cleanText(latest && latest.status) || "future";
      item.append(
        element("span", "execution-stage-index", String(timeline.childElementCount + 1)),
        element("strong", "", executionStageLabel(stageName)),
        element(
          "span",
          "",
          latest ? executionStatusLabel(latest.status) : "尚未开始",
        ),
      );
      if (latest) {
        item.appendChild(
          element("small", "", cleanText(latest.reason_code) || "—"),
        );
      }
      timeline.appendChild(item);
    });
    root.appendChild(timeline);

    if (latestRun) {
      const jobBar = element("div", "execution-job-bar");
      const jobCopy = element("div");
      jobCopy.append(
        element(
          "strong",
          "",
          `${executionStageLabel(latestRun.stage)} · ${executionStatusLabel(latestJob.state)}`,
        ),
        element(
          "span",
          "",
          cleanText(latestJob.progress_message)
            || `Run ${latestRun.stage_run_number}/${latestRun.max_stage_runs}`,
        ),
      );
      jobBar.appendChild(jobCopy);
      if (
        !executionJobTerminal(latestJob.state)
        && !latestJob.cancel_requested_at
      ) {
        const cancel = analysisAction("请求取消", async () => {
          cancel.disabled = true;
          cancel.textContent = "正在记录取消…";
          try {
            await ensureLocalAccessToken();
            await requestJSON(
              `${API.jobs}/${encodeURIComponent(latestRun.job_id)}/cancel`,
              {
                method: "POST",
                headers: mutationHeaders(),
              },
            );
            showToast("取消请求已保存，将在当前步骤结束后生效。");
            await refresh();
          } catch (error) {
            showToast(`取消失败：${friendlyError(error)}`, true);
            cancel.disabled = false;
            cancel.textContent = "请求取消";
          }
        });
        cancel.classList.add("is-danger", "is-compact");
        jobBar.appendChild(cancel);
      } else if (latestJob.cancel_requested_at) {
        jobBar.appendChild(
          element("span", "execution-cancel-pending", "取消请求已记录"),
        );
      }
      root.appendChild(jobBar);
    }

    const overview = element("div", "execution-detail-grid");
    overview.append(
      buildExecutionPolicy(detail),
      buildExecutionCommands(plan),
      buildExecutionRunResources(runs),
    );
    const advancedOverview = document.createElement("details");
    advancedOverview.className = "technical-details execution-advanced";
    advancedOverview.append(
      element("summary", "", "高级执行信息"),
      overview,
    );
    root.appendChild(advancedOverview);

    const artifacts = manifests.flatMap((manifest) => (
      Array.isArray(manifest.entries)
        ? manifest.entries.map((entry) => ({
            ...entry,
            stage: manifest.stage,
            manifest_hash: manifest.manifest_hash,
          }))
        : []
    ));
    const evidence = artifacts.filter((item) => item.role === "stage-result");
    const inventory = artifacts.find((item) => item.role === "file-inventory");
    const diff = artifacts.find((item) => item.role === "unified-diff");
    const tests = artifacts.find((item) => item.role === "normalized-test-results");
    const artifactGrid = element("div", "execution-artifact-grid");
    if (diff) {
      artifactGrid.appendChild(
        buildExecutionArtifactCard(
          diff,
          "代码变更",
          "查看本次修改的具体内容。",
          renderExecutionDiff,
        ),
      );
    }
    if (tests) {
      artifactGrid.appendChild(
        buildExecutionArtifactCard(
          tests,
          "测试结果",
          "查看各项检查是否通过及耗时。",
          renderExecutionTests,
        ),
      );
    }
    if (inventory) {
      artifactGrid.appendChild(
        buildExecutionArtifactCard(
          inventory,
          "文件变化",
          "查看新增、修改和删除的文件。",
          renderExecutionInventory,
        ),
      );
    }
    evidence.forEach((entry) => {
      artifactGrid.appendChild(
        buildExecutionArtifactCard(
          entry,
          `${executionStageLabel(entry.stage)} 运行记录`,
          "查看详细命令、资源和已脱敏日志。",
          renderExecutionEvidence,
        ),
      );
    });
    if (!artifactGrid.childElementCount) {
      artifactGrid.appendChild(
        analysisMessage(
          "结果尚未生成",
          cleanText(current.status) === "pending"
            ? "任务仍在等待开始，完成后会在这里显示结果。"
            : "当前步骤尚未产生可查看的结果。",
        ),
      );
    }
    const artifactSection = element("section", "execution-artifacts");
    artifactSection.append(
      planningCardHeading(
        "执行结果",
        `${artifacts.length} 项可查看内容`,
      ),
      artifactGrid,
    );
    root.appendChild(artifactSection);

    const provenance = document.createElement("details");
    provenance.className = "execution-provenance";
    provenance.appendChild(element("summary", "", "执行溯源与哈希"));
    const list = element("dl", "");
    appendMetadata(list, "Execution", detail.id);
    appendMetadata(list, "Attempt hash", detail.record_hash);
    appendMetadata(list, "Plan", `${detail.plan_version_id} · ${detail.plan_record_hash}`);
    appendMetadata(list, "Approval", detail.approval_hash);
    appendMetadata(list, "Base", detail.base_commit_sha);
    appendMetadata(list, "Archive", detail.repository_archive_hash);
    appendMetadata(list, "Runner", detail.runner_image_digest);
    appendMetadata(
      list,
      "Sandbox policy",
      `${detail.sandbox_policy_version} · ${detail.sandbox_policy_hash}`,
    );
    provenance.appendChild(list);
    root.appendChild(provenance);
  }

  function buildVibeCodingPanel(detail, refreshExecution) {
    const card = element("section", "execution-subcard vibe-coding-panel");
    const title = element("div", "vibe-coding-heading");
    title.append(
      element("h6", "", `${state.implementationModel} Vibe Coding`),
      element(
        "p",
        "review-meta",
        "模型只看到批准路径的冻结内容，不持有 Docker、GitHub 或 NVIDIA 密钥。",
      ),
    );
    const body = element("div", "vibe-coding-body");
    body.setAttribute("aria-live", "polite");
    body.replaceChildren(
      analysisMessage("正在读取编码会话", "正在复验上下文与对话哈希。", true),
    );
    card.append(title, body);

    const loadSession = async () => {
      try {
        const session = await requestJSON(
          `${API.executions}/${encodeURIComponent(detail.id)}/coding-session`,
        );
        if (!card.isConnected) return;
        renderVibeCodingSession(body, detail, session, loadSession, refreshExecution);
      } catch (error) {
        if (!card.isConnected) return;
        if (error.status !== 404) {
          const failure = analysisMessage(
            "编码会话不可用",
            friendlyError(error),
          );
          const retry = analysisAction("重新读取", () => loadSession());
          failure.appendChild(retry);
          body.replaceChildren(failure);
          return;
        }
        const empty = analysisMessage(
          "尚未冻结编码上下文",
          "先让 Sandbox Worker 从精确归档中只读提取批准路径；仓库代码不会在 API 主机执行。",
        );
        const start = analysisAction("开始 Vibe Coding", async () => {
          start.disabled = true;
          start.textContent = "正在冻结上下文…";
          try {
            await ensureLocalAccessToken();
            const queued = await requestJSON(
              `${API.executions}/${encodeURIComponent(detail.id)}/coding-context`,
              {
                method: "POST",
                headers: mutationHeaders({
                  "Idempotency-Key": randomKey("web-coding-context"),
                }),
              },
            );
            const completed = await waitForTerminalJob(queued);
            if (cleanText(completed.state) !== "succeeded") {
              throw new Error(completed.error_message || "编码上下文提取失败");
            }
            await loadSession();
          } catch (error) {
            empty.appendChild(
              element("p", "planning-form-status", friendlyError(error)),
            );
            start.disabled = false;
            start.textContent = "重试冻结上下文";
          }
        });
        empty.appendChild(start);
        body.replaceChildren(empty);
      }
    };
    loadSession();
    return card;
  }

  function renderVibeCodingSession(
    root,
    detail,
    session,
    reload,
    refreshExecution,
  ) {
    root.replaceChildren();
    const meta = element("div", "vibe-coding-meta");
    meta.append(
      element("span", "", `上下文 ${shortHash(session.context_hash)}`),
      element("span", "", `Explore ${shortHash(session.explore_result_hash)}`),
      element("span", "", `Base ${shortHash(session.base_commit_sha)}`),
    );
    root.appendChild(meta);

    const turns = Array.isArray(session.turns) ? session.turns : [];
    const conversation = element("ol", "vibe-coding-conversation");
    turns.forEach((turn) => {
      const item = element("li", "vibe-coding-turn");
      item.dataset.role = cleanText(turn.role);
      item.append(
        element(
          "strong",
          "",
          cleanText(turn.role) === "assistant" ? state.implementationModel : "你",
        ),
        element("p", "", cleanText(turn.content)),
        element("small", "", shortHash(turn.record_hash)),
      );
      conversation.appendChild(item);
    });
    if (!turns.length) {
      conversation.appendChild(
        element(
          "li",
          "planning-empty-copy",
          `上下文已冻结。描述你想实现的行为、边界或测试，${state.implementationModel} 会基于批准文件与你逐步确认。`,
        ),
      );
    }
    root.appendChild(conversation);

    const form = element("form", "vibe-coding-form");
    const message = document.createElement("textarea");
    message.className = "plan-input";
    message.required = true;
    message.maxLength = 12000;
    message.rows = 4;
    message.placeholder = "例如：先补充失败测试，再最小化修改解析逻辑；不要改公共 API。";
    message.setAttribute("aria-label", "Vibe Coding 消息");
    const send = analysisAction(`发送给 ${state.implementationModel}`, () => {});
    send.type = "submit";
    const status = element("span", "planning-form-status");
    form.append(message, send, status);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      send.disabled = true;
      status.textContent = `${state.implementationModel} 正在分析冻结上下文…`;
      try {
        await ensureLocalAccessToken();
        const queued = await requestJSON(
          `${API.executions}/${encodeURIComponent(detail.id)}/coding/messages`,
          {
            method: "POST",
            headers: mutationHeaders({
              "Content-Type": "application/json",
              "Idempotency-Key": randomKey("web-coding-turn"),
            }),
            body: JSON.stringify({ content: message.value.trim() }),
          },
        );
        const completed = await waitForTerminalJob(queued);
        if (cleanText(completed.state) !== "succeeded") {
          throw new Error(completed.error_message || `${state.implementationModel} 编码消息失败`);
        }
        await reload();
      } catch (error) {
        status.textContent = friendlyError(error);
        send.disabled = false;
      }
    });
    root.appendChild(form);

    const latest = turns.length ? turns[turns.length - 1] : null;
    if (latest && cleanText(latest.role) === "assistant") {
      const proposalBar = element("div", "vibe-coding-proposal-bar");
      const propose = analysisAction("生成可确认的 ChangeSet", async () => {
        propose.disabled = true;
        proposalStatus.textContent = `${state.implementationModel} 正在生成结构化文件操作…`;
        try {
          await ensureLocalAccessToken();
          const queued = await requestJSON(
            `${API.executions}/${encodeURIComponent(detail.id)}/coding/proposals`,
            {
              method: "POST",
              headers: mutationHeaders({
                "Idempotency-Key": randomKey("web-coding-proposal"),
              }),
            },
          );
          const completed = await waitForTerminalJob(queued);
          if (cleanText(completed.state) !== "succeeded") {
            throw new Error(completed.error_message || "ChangeSet 生成失败");
          }
          await reload();
        } catch (error) {
          proposalStatus.textContent = friendlyError(error);
          propose.disabled = false;
        }
      });
      const proposalStatus = element("span", "planning-form-status");
      proposalBar.append(propose, proposalStatus);
      root.appendChild(proposalBar);
    }

    const proposals = Array.isArray(session.proposals) ? session.proposals : [];
    proposals.forEach((proposal) => {
      const proposalCard = element("article", "vibe-coding-proposal");
      proposalCard.append(
        element("strong", "", cleanText(proposal.summary) || "ChangeSet 提案"),
        element(
          "p",
          "review-meta",
          `${toStringArray(proposal.paths).join(" · ")} · ${shortHash(proposal.change_set_hash)}`,
        ),
      );
      const confirmation = element("label", "vibe-coding-confirmation");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      confirmation.append(
        checkbox,
        document.createTextNode(
          ` 我确认将精确 ChangeSet ${shortHash(proposal.change_set_hash)} 交给隔离 Implement`,
        ),
      );
      const accept = analysisAction("确认并执行", async () => {
        if (!checkbox.checked) {
          proposalStatus.textContent = "请先确认页面显示的精确 ChangeSet 哈希。";
          return;
        }
        accept.disabled = true;
        proposalStatus.textContent = "正在复验提案、对话和文件 prior hash…";
        try {
          await ensureLocalAccessToken();
          await requestJSON(
            `/api/v1/change-set-proposals/${encodeURIComponent(proposal.id)}/accept`,
            {
              method: "POST",
              headers: mutationHeaders({
                "Content-Type": "application/json",
                "Idempotency-Key": randomKey("web-accept-proposal"),
              }),
              body: JSON.stringify({
                action: "accept_change_set",
                expected_change_set_hash: proposal.change_set_hash,
              }),
            },
          );
          showToast("精确 ChangeSet 已确认，隔离 Implement 等待调度。");
          await refreshExecution();
        } catch (error) {
          proposalStatus.textContent = friendlyError(error);
          accept.disabled = false;
        }
      });
      const proposalStatus = element("span", "planning-form-status");
      proposalCard.append(confirmation, accept, proposalStatus);
      root.appendChild(proposalCard);
    });
  }

  function buildExecutionPolicy(detail) {
    const card = element("section", "execution-subcard");
    card.appendChild(element("h6", "", "沙箱策略"));
    const list = element("ul", "execution-fact-list");
    [
      `${cleanText(detail.sandbox_policy_version) || "policy"} · ${shortHash(detail.sandbox_policy_hash)}`,
      "上限：2 CPU · 2 GiB · 256 PIDs · 600 秒",
      "network=none · root filesystem 只读",
      "非 root 65532:65532 · cap-drop ALL",
      "无 Docker socket / HOME / SSH / 模型凭据",
    ].forEach((value) => list.appendChild(element("li", "", value)));
    card.appendChild(list);
    return card;
  }

  function buildExecutionCommands(plan) {
    const card = element("section", "execution-subcard");
    card.appendChild(element("h6", "", "批准命令"));
    const commands = Array.isArray(plan && plan.commands_to_run)
      ? plan.commands_to_run
      : [];
    if (!commands.length) {
      card.appendChild(element("p", "planning-empty-copy", "没有批准命令。"));
      return card;
    }
    const list = element("ol", "execution-command-list");
    commands.forEach((command) => {
      const item = document.createElement("li");
      item.append(
        element("strong", "", cleanText(command.command_id) || "command"),
        element("span", "", cleanText(command.purpose) || "—"),
        element("code", "", commandArgv(command.argv)),
        element("small", "", `cwd ${cleanText(command.working_directory) || "."}`),
      );
      list.appendChild(item);
    });
    card.appendChild(list);
    return card;
  }

  function buildExecutionRunResources(runs) {
    const card = element("section", "execution-subcard");
    card.appendChild(element("h6", "", "StageRun / Job"));
    if (!runs.length) {
      card.appendChild(element("p", "planning-empty-copy", "尚未创建 StageRun。"));
      return card;
    }
    const list = element("ul", "execution-fact-list");
    runs.forEach((run) => {
      const job = objectValue(run.job);
      list.appendChild(
        element(
          "li",
          "",
          `${executionStageLabel(run.stage)} #${run.stage_run_number} · `
          + `${executionStatusLabel(job.state)} · ${run.timeout_seconds}s · `
          + `progress ${job.progress_current}/${job.progress_total ?? "—"}`,
        ),
      );
    });
    card.appendChild(list);
    return card;
  }

  function buildExecutionArtifactCard(entry, title, copy, renderer) {
    const card = element("article", "execution-artifact-card");
    card.append(
      element("h6", "", title),
      element("p", "", copy),
      element(
        "small",
        "",
        `${formatBytes(entry.size_bytes)} · ${shortHash(entry.artifact_id)}`,
      ),
    );
    const output = element("div", "execution-artifact-output");
    const button = analysisAction("查看结果", async () => {
      const url = executionArtifactUrl(entry.content_url);
      if (!url) {
        output.replaceChildren(
          analysisMessage("结果地址无效", "无法读取这项执行结果。"),
        );
        return;
      }
      button.disabled = true;
      output.replaceChildren(
        analysisMessage("正在读取结果", "正在确认内容完整性。", true),
      );
      try {
        const artifact = await requestArtifact(url);
        renderer(output, artifact.value, artifact.text);
        button.textContent = "重新加载";
      } catch (error) {
        output.replaceChildren(
          analysisMessage("结果读取失败", friendlyError(error)),
        );
      } finally {
        button.disabled = false;
      }
    });
    button.classList.add("is-compact");
    card.append(button, output);
    return card;
  }

  function renderExecutionDiff(root, _value, text) {
    const diff = element("pre", "execution-diff");
    diff.textContent = text || "空 diff";
    root.replaceChildren(diff);
  }

  function renderExecutionTests(root, value) {
    const tests = Array.isArray(value) ? value : [];
    if (!tests.length) {
      root.replaceChildren(
        element("p", "planning-empty-copy", "没有规范化测试结果。"),
      );
      return;
    }
    const list = element("div", "execution-test-list");
    tests.forEach((test) => {
      const item = element("article", "execution-test-result");
      item.dataset.status = cleanText(test.outcome);
      item.append(
        element("strong", "", cleanText(test.command_id) || "command"),
        element("span", "", executionStatusLabel(test.outcome)),
        element(
          "small",
          "",
          `exit ${test.exit_code ?? "—"} · ${formatDuration(test.duration_ms)} · `
          + `${formatBytes(test.stdout_bytes)} stdout / ${formatBytes(test.stderr_bytes)} stderr`,
        ),
      );
      list.appendChild(item);
    });
    root.replaceChildren(list);
  }

  function renderExecutionInventory(root, value) {
    const inventory = objectValue(value);
    const wrapper = element("div", "execution-inventory");
    wrapper.append(
      analysisMetric("变更路径", formatNumber(toStringArray(inventory.changed_paths).length)),
      analysisMetric("基线文件", formatNumber(toFiniteNumber(inventory.baseline_file_count, 0))),
      analysisMetric("结果文件", formatNumber(toFiniteNumber(inventory.result_file_count, 0))),
    );
    const paths = toStringArray(inventory.changed_paths);
    if (paths.length) {
      const list = element("ul", "execution-fact-list");
      paths.forEach((path) => list.appendChild(element("li", "", path)));
      wrapper.appendChild(list);
    }
    root.replaceChildren(wrapper);
  }

  function renderExecutionEvidence(root, value) {
    const evidence = objectValue(value);
    const commands = Array.isArray(evidence.command_evidence)
      ? evidence.command_evidence
      : [];
    if (!commands.length) {
      const summary = element("dl", "execution-evidence-summary");
      appendMetadata(summary, "Result version", evidence.version);
      appendMetadata(summary, "Result hash", evidence.result_hash || evidence.spec_hash);
      appendMetadata(summary, "Inventory", evidence.result_inventory_hash || evidence.inventory_hash);
      root.replaceChildren(summary);
      return;
    }
    const list = element("div", "execution-evidence-list");
    commands.forEach((command) => {
      const item = element("article", "execution-command-evidence");
      const usage = objectValue(command.resource_usage);
      item.append(
        element("strong", "", cleanText(command.command_id) || "command"),
        element(
          "span",
          "",
          `${executionStatusLabel(command.status)} · exit ${command.exit_code ?? "—"} · `
          + formatDuration(command.duration_ms),
        ),
        element(
          "small",
          "",
          `CPU user ${formatCpuMillis(usage.user_cpu_ms)} · `
          + `system ${formatCpuMillis(usage.system_cpu_ms)} · `
          + `RSS ${formatBytes(usage.max_rss_bytes)}`,
        ),
      );
      const stdout = cleanText(command.stdout_log);
      const stderr = cleanText(command.stderr_log);
      if (stdout || stderr) {
        const logs = document.createElement("details");
        logs.appendChild(element("summary", "", "脱敏日志"));
        const pre = element("pre", "execution-log");
        pre.textContent = [stdout, stderr].filter(Boolean).join("\n");
        logs.appendChild(pre);
        item.appendChild(logs);
      }
      list.appendChild(item);
    });
    root.replaceChildren(list);
  }

  function buildReviewCard(review, refresh, automated = false) {
    const card = element("section", "execution-subcard review-card");
    const stale = Boolean(review.stale);
    card.append(
      element(
        "h6",
        "",
        stale ? "Review 已过期" : `Review · ${cleanText(review.verdict) === "pass" ? "通过" : "阻断"}`,
      ),
      element(
        "p",
        "review-meta",
        `绑定 ${shortHash(review.binding_hash)} · ${cleanText(review.reason_code)}`,
      ),
    );
    const findings = Array.isArray(review.findings) ? review.findings : [];
    const list = element("ul", "review-finding-list");
    findings.forEach((finding) => {
      const item = element("li", "review-finding");
      item.append(
        element("strong", "", `${cleanText(finding.severity)} · ${cleanText(finding.location)}`),
        element("span", "", cleanText(finding.evidence)),
        element("em", "", cleanText(finding.recommendation)),
      );
      list.appendChild(item);
    });
    card.appendChild(list);
    if (!automated && cleanText(review.verdict) === "block" && !stale) {
      const repairForm = element("form", "planning-execution-form");
      const repair = analysisAction("启动有界修复", () => {});
      repair.type = "submit";
      const status = element("span", "planning-form-status");
      repairForm.append(repair, status);
      repairForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        repair.disabled = true;
        status.textContent = "正在创建新的 ExecutionAttempt…";
        try {
          await ensureLocalAccessToken();
          await requestJSON(
            `${API.reviews}/${encodeURIComponent(review.id)}/repair`,
            {
              method: "POST",
              headers: mutationHeaders({
                "Content-Type": "application/json",
                "Idempotency-Key": randomKey("web-repair"),
              }),
              body: JSON.stringify({ actor_id: "local-user" }),
            },
          );
          showToast("有界修复已启动，请重新跑 Explore → Verify。");
          refresh();
        } catch (error) {
          status.textContent = friendlyError(error);
          repair.disabled = false;
        }
      });
      card.appendChild(repairForm);
    }
    if (!automated && cleanText(review.verdict) === "pass" && !stale) {
      card.appendChild(buildPublishForm(review, refresh));
    }
    return card;
  }

  function buildPublishForm(review, refresh) {
    const form = element("form", "planning-execution-form");
    const title = element("input", "plan-input");
    title.placeholder = "Draft PR 标题";
    title.value = "contribos: apply reviewed change";
    const body = element("textarea", "plan-input");
    body.placeholder = "Draft PR 说明";
    body.value = "Bound to the exact reviewed plan, diff, tests, and policy hashes.";
    const submit = analysisAction(
      state.draftPrPublisher === "fake" ? "创建 Fake 发布意图" : "创建发布意图",
      () => {},
    );
    submit.type = "submit";
    const status = element("span", "planning-form-status");
    form.append(title, body, submit, status);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      submit.disabled = true;
      status.textContent = "正在创建一次性发布意图…";
      try {
        await ensureLocalAccessToken();
        const intent = await requestJSON(
          `${API.reviews}/${encodeURIComponent(review.id)}/publish-intents`,
          {
            method: "POST",
            headers: mutationHeaders({
              "Content-Type": "application/json",
              "Idempotency-Key": randomKey("web-publish-intent"),
            }),
            body: JSON.stringify({
              actor_id: "local-user",
              title: title.value,
              body: body.value,
            }),
          },
        );
        showToast("发布意图已创建，请确认后才会写远程。");
        form.replaceWith(buildConfirmPublish(intent, refresh));
      } catch (error) {
        status.textContent = friendlyError(error);
        submit.disabled = false;
      }
    });
    return form;
  }

  function buildConfirmPublish(intent, refresh) {
    const form = element("form", "planning-execution-form");
    form.append(
      element("p", "review-meta", `${cleanText(intent.upstream_repository)} · ${cleanText(intent.head_branch)}`),
      element("p", "review-meta", `动作：${toStringArray(intent.allowed_actions).join(", ") || "create_draft_pr"}`),
    );
    const submit = analysisAction(
      state.draftPrPublisher === "fake"
        ? "确认发布 Draft PR（本地演示）"
        : "确认发布 Draft PR",
      () => {},
    );
    submit.type = "submit";
    const status = element("span", "planning-form-status");
    form.append(submit, status);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      submit.disabled = true;
      status.textContent = "正在确认并记录演示 Draft PR…";
      try {
        await ensureLocalAccessToken();
        const result = await requestJSON(
          `${API.publishIntents}/${encodeURIComponent(intent.id)}/confirm`,
          {
            method: "POST",
            headers: mutationHeaders({
              "Content-Type": "application/json",
            }),
            body: JSON.stringify({
              actor_id: "local-user",
              confirmation_nonce: intent.confirmation_nonce,
            }),
          },
        );
        const draft = objectValue(result.draft_pull_request);
        showToast(`演示 Draft PR #${draft.number || "?"} 已记录。`);
        refresh();
      } catch (error) {
        status.textContent = friendlyError(error);
        submit.disabled = false;
      }
    });
    return form;
  }

  function buildLifecyclePanel(task, currentState, refresh) {
    const section = element("section", "planning-card");
    section.appendChild(
      planningCardHeading("任务生命周期", "侧态与奖励由用户显式记录，不会自动写支付或 GitHub。"),
    );
    const state = cleanText(currentState.to_state);
    const actions = [];
    if (state === "changes_requested") {
      actions.push(["revise", "根据评审意见修订计划"]);
    }
    if (state === "merged") {
      actions.push(["reward", "标记已获奖"]);
    }
    if (!["rewarded", "failed", "rejected", "abandoned"].includes(state)) {
      actions.push(["abandon", "放弃任务"]);
    }
    if (state === "executing") {
      actions.push(["fail", "标记执行失败"]);
    }
    if (state === "reviewing") {
      actions.push(["reject", "拒绝此次 Review"]);
    }
    if (!actions.length) {
      section.appendChild(element("p", "planning-card-copy", `当前状态：${state || "未知"}`));
      return section;
    }
    actions.forEach(([action, label]) => {
      const button = analysisAction(label, async () => {
        try {
          await ensureLocalAccessToken();
          await requestJSON(
            `${API.tasks}/${encodeURIComponent(task.id)}/lifecycle`,
            {
              method: "POST",
              headers: mutationHeaders({
                "Content-Type": "application/json",
              }),
              body: JSON.stringify({ action, actor_id: "local-user" }),
            },
          );
          showToast("任务状态已更新。");
          refresh();
        } catch (error) {
          showToast(friendlyError(error), true);
        }
      });
      section.appendChild(button);
    });
    return section;
  }

  function renderContributionDashboard(data) {
    if (!dom.funnelGrid || !dom.heatmapGrid || !dom.taskHistory) {
      return;
    }
    const funnel = objectValue(data.funnel);
    const metrics = objectValue(data.metrics);
    if (toFiniteNumber(metrics.task_count, 0) === 0) {
      dom.funnelGrid.replaceChildren(
        analysisMessage(
          "还没有进行中的贡献",
          "从候选机会运行深入评估并创建贡献任务后，这里会显示下一步和成果进度。",
        ),
      );
      dom.heatmapGrid.hidden = true;
      renderTaskHistory();
      return;
    }
    dom.heatmapGrid.hidden = false;
    const labels = [
      ["scanned", "已扫描"],
      ["analyzed", "已深析"],
      ["planned", "已计划"],
      ["executed", "已执行"],
      ["reviewed", "已评审"],
      ["submitted", "已提交"],
      ["merged", "已合并"],
      ["rewarded", "已获奖"],
    ];
    dom.funnelGrid.replaceChildren();
    labels.forEach(([key, label]) => {
      const card = element("article", "funnel-card");
      card.append(
        element("span", "", label),
        element("strong", "", formatNumber(toFiniteNumber(funnel[key], 0))),
      );
      dom.funnelGrid.appendChild(card);
    });
    const heatmap = Array.isArray(data.heatmap) ? data.heatmap : [];
    dom.heatmapGrid.replaceChildren();
    if (!heatmap.length) {
      dom.heatmapGrid.appendChild(element("p", "", "还没有可追溯的贡献事件。"));
    } else {
      heatmap.slice(-14).forEach((day) => {
        const item = element("article", "heatmap-day");
        item.append(
          element("strong", "", cleanText(day.date)),
          element(
            "span",
            "",
            `贡献 ${toFiniteNumber(day.contributions, 0)} · PR ${toFiniteNumber(day.pull_requests, 0)} · 合并 ${toFiniteNumber(day.merges, 0)} · 奖励 ${toFiniteNumber(day.rewards, 0)}`,
          ),
        );
        dom.heatmapGrid.appendChild(item);
      });
    }
    renderTaskHistory();
  }

  async function renderTaskHistory() {
    if (!dom.taskHistory) {
      return;
    }
    try {
      const tasks = await requestJSON(API.tasks);
      dom.taskHistory.replaceChildren();
      const rows = Array.isArray(tasks) ? tasks : [];
      state.tasks = rows;
      if (!rows.length) {
        dom.taskHistory.appendChild(element("p", "", "还没有贡献任务。"));
        return;
      }
      rows.slice(-12).reverse().forEach((task) => {
        const item = element("article", "task-history-item");
        const progress = document.createElement("progress");
        progress.max = 100;
        progress.value = toFiniteNumber(task.progress_percent, 0);
        progress.setAttribute("aria-label", `贡献进度 ${progress.value}%`);
        item.append(
          element("span", "task-repository", cleanText(task.repository_full_name) || "开源贡献"),
          element("strong", "", cleanText(task.opportunity_title) || "贡献任务"),
          element("span", "task-friendly-state", cleanText(task.friendly_state) || humanize(task.current_state)),
          progress,
          element("span", "task-next-action", cleanText(task.next_action) || "当前阶段已完成"),
        );
        const actions = element("div", "task-history-actions");
        const open = analysisAction("继续贡献", () => openContributionTask(task));
        open.classList.add("is-compact");
        actions.appendChild(open);
        item.appendChild(actions);
        dom.taskHistory.appendChild(item);
      });
    } catch (error) {
      dom.taskHistory.replaceChildren(
        element("p", "", `任务历史暂不可用：${friendlyError(error)}`),
      );
    }
  }

  async function openContributionTask(summary) {
    const taskId = cleanText(summary && summary.id);
    const opportunityId = Math.trunc(
      toFiniteNumber(summary && summary.opportunity_id, 0),
    );
    const analysisVersionId = cleanText(summary && summary.analysis_version_id);
    if (!taskId || opportunityId < 1 || !analysisVersionId) {
      showToast("任务缺少可验证的分析来源，无法打开工作台。", true);
      return;
    }
    window.location.hash = "#/contributions";
    dom.contributionWorkbench.hidden = false;
    const heading = element("header", "contribution-workbench-heading");
    const copy = element("div");
    copy.append(
      element("span", "planning-kicker", "VIBE CODING WORKBENCH"),
      element("h3", "", cleanText(summary.opportunity_title) || "继续代码贡献"),
      element("p", "", "计划、隔离执行、Review 与 Draft PR 意图都绑定到不可变输入。"),
    );
    const close = analysisAction("收起工作台", () => {
      dom.contributionWorkbench.hidden = true;
      dom.contributionWorkbench.replaceChildren();
    });
    close.classList.add("is-secondary", "is-compact");
    heading.append(copy, close);
    const body = element("div", "contribution-workbench-body");
    body.appendChild(
      analysisMessage("正在打开贡献任务", "正在校验 Analysis、Plan 和执行记录…", true),
    );
    dom.contributionWorkbench.replaceChildren(heading, body);
    dom.contributionWorkbench.scrollIntoView({ behavior: "smooth", block: "start" });
    try {
      const [taskDetail, analysisDetail] = await Promise.all([
        requestJSON(`${API.tasks}/${encodeURIComponent(taskId)}`),
        requestJSON(
          `${API.opportunities}/${encodeURIComponent(opportunityId)}`
          + `/analyses/${encodeURIComponent(analysisVersionId)}`,
        ),
      ]);
      const panel = {
        taskIds: new Map([[analysisVersionId, taskId]]),
      };
      renderPlanningWorkbench(body, panel, analysisDetail, taskDetail);
    } catch (error) {
      body.replaceChildren(
        analysisMessage("贡献工作台载入失败", friendlyError(error)),
      );
    }
  }

  function executionArtifactUrl(value) {
    const path = cleanText(value);
    return /^\/api\/v1\/executions\/[A-Za-z0-9-]+\/artifacts\/[0-9a-f]{64}$/.test(path)
      ? path
      : null;
  }

  function executionJobTerminal(value) {
    return ["succeeded", "failed", "cancelled", "timed_out"].includes(
      cleanText(value).toLowerCase(),
    );
  }

  function executionStageLabel(value) {
    const key = cleanText(value).toLowerCase();
    return EXECUTION_STAGE_LABELS[key] || humanize(key) || "—";
  }

  function executionStatusLabel(value) {
    const key = cleanText(value).toLowerCase();
    return EXECUTION_STATUS_LABELS[key] || humanize(key) || "未知";
  }

  function commandArgv(value) {
    if (!Array.isArray(value)) return "—";
    return value.map((item) => JSON.stringify(String(item))).join(" ");
  }

  function formatBytes(value) {
    const bytes = Math.max(0, toFiniteNumber(value, 0));
    if (bytes < 1024) return `${formatNumber(bytes)} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
    if (bytes < 1024 * 1024 * 1024) {
      return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
    }
    return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GiB`;
  }

  function formatCpuMillis(value) {
    const milliseconds = Math.max(0, toFiniteNumber(value, 0));
    return `${(milliseconds / 1000).toFixed(3)}s`;
  }

  function buildPlanningProvenance(task, currentState, lock) {
    const details = document.createElement("details");
    details.className = "planning-card planning-provenance";
    details.appendChild(element("summary", "", "任务与锁定溯源"));
    const list = element("dl", "");
    appendMetadata(list, "Task", task.id);
    appendMetadata(list, "Task hash", task.record_hash);
    appendMetadata(list, "Analysis", task.analysis_version_id);
    appendMetadata(list, "Snapshot", task.snapshot_id);
    appendMetadata(list, "State", `${currentState.to_state} · ${currentState.record_hash}`);
    appendMetadata(list, "Plan lock", lock ? lock.lock_hash : "尚未锁定");
    appendMetadata(list, "Provider/Policy", lock ? lock.provider_contract_hash : "—");
    details.appendChild(list);
    return details;
  }

  function planningStatus(label, value, tone) {
    const item = element("div", "planning-status");
    item.dataset.tone = tone || "";
    item.append(element("span", "", label), element("strong", "", value));
    return item;
  }

  function planningApprovalLabel(status) {
    return {
      approved: "已批准",
      revoked: "已失效",
      unapproved: "未批准",
    }[cleanText(status)] || "未知";
  }

  function planningCardHeading(title, copy) {
    const heading = element("header", "planning-card-heading");
    heading.append(element("h5", "", title), element("p", "", copy));
    return heading;
  }

  function planningTextField(label, name, value, rows, extraClass) {
    const field = element("label", "planning-field");
    field.appendChild(element("span", "", label));
    const textarea = document.createElement("textarea");
    textarea.name = name;
    textarea.rows = rows;
    textarea.value = value || "";
    textarea.required = !["risks", "questions_for_maintainer"].includes(name);
    if (extraClass) textarea.classList.add(extraClass);
    field.appendChild(textarea);
    return field;
  }

  function planFormPayload(form, parentVersionId) {
    const fields = form.elements;
    let commands;
    try {
      commands = JSON.parse(fields.commands_to_run.value);
    } catch (_error) {
      throw new Error("命令 JSON 无法解析");
    }
    if (!Array.isArray(commands)) throw new Error("命令必须是 JSON 数组");
    return {
      parent_version_id: parentVersionId || null,
      goal: fields.goal.value.trim(),
      acceptance_criteria: splitLines(fields.acceptance_criteria.value),
      files_to_inspect: splitLines(fields.files_to_inspect.value),
      files_likely_to_change: splitLines(fields.files_likely_to_change.value),
      implementation_steps: splitLines(fields.implementation_steps.value),
      tests_to_add_or_run: splitLines(fields.tests_to_add_or_run.value),
      commands_to_run: commands,
      risks: splitLines(fields.risks.value),
      questions_for_maintainer: splitLines(fields.questions_for_maintainer.value),
    };
  }

  function splitLines(value) {
    return String(value || "")
      .split(/\r?\n/)
      .map((item) => item.trim())
      .filter(Boolean);
  }

  function randomKey(prefix) {
    const suffix = window.crypto && typeof window.crypto.randomUUID === "function"
      ? window.crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    return `${prefix}-${suffix}`;
  }

  function shortHash(value) {
    const text = cleanText(value);
    return text.length > 12 ? `${text.slice(0, 8)}…${text.slice(-4)}` : text || "—";
  }

  async function runAnalysis(panel, retryJobId) {
    if (!retryJobId && (!state.analysisReady || !panel.snapshotId)) return;
    try {
      await ensureLocalAccessToken();
      const idempotencyKey = window.crypto && typeof window.crypto.randomUUID === "function"
        ? window.crypto.randomUUID()
        : `web-analysis-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      const queued = retryJobId
        ? await requestJSON(`${API.jobs}/${encodeURIComponent(retryJobId)}/retry`, {
            method: "POST",
            headers: mutationHeaders(),
          })
        : await requestJSON(analysisEndpoint(panel), {
            method: "POST",
            headers: mutationHeaders({
              "Content-Type": "application/json",
              "Idempotency-Key": idempotencyKey,
            }),
            body: JSON.stringify({ snapshot_id: panel.snapshotId }),
          });
      panel.activeJobId = cleanText(queued.id);
      panel.retryJobId = null;
      renderAnalysisJobProgress(panel, queued);
      const completed = await waitForAnalysisJob(panel, queued);
      if (completed.state !== "succeeded") {
        panel.retryJobId = cleanText(completed.id);
        throw new Error(
          cleanText(completed.error_message)
          || `分析任务已结束（${cleanText(completed.state) || "unknown"}）`,
        );
      }
      panel.activeJobId = null;
      panel.loaded = false;
      showToast("深度分析完成，已生成新的不可变版本。");
      await loadAnalysisHistory(panel, true);
    } catch (error) {
      panel.activeJobId = null;
      setAnalysisStatus(panel, "分析失败", "error");
      renderAnalysisError(panel, "深度分析未完成", friendlyError(error), {
        retryJob: panel.retryJobId,
        retryHistory: true,
      });
    }
  }

  async function waitForAnalysisJob(panel, initialJob) {
    let job = initialJob;
    let revision = null;
    const deadline = Date.now()
      + Math.max(60, toFiniteNumber(job.timeout_seconds, 600) + 90) * 1000;
    const terminal = new Set(["succeeded", "failed", "cancelled", "timed_out"]);
    while (!terminal.has(cleanText(job.state).toLowerCase())) {
      if (Date.now() >= deadline) {
        throw new Error("分析仍在运行，任务已保留，可稍后继续查看。");
      }
      await sleep(1000);
      const eventQuery = revision ? `?after_revision=${encodeURIComponent(revision)}` : "";
      const [nextJob, feed] = await Promise.all([
        requestJSON(`${API.jobs}/${encodeURIComponent(job.id)}`),
        requestJSON(`${API.jobs}/${encodeURIComponent(job.id)}/events${eventQuery}`),
      ]);
      job = nextJob;
      revision = cleanText(feed.revision) || revision;
      renderAnalysisJobProgress(panel, job, feed.events);
    }
    return job;
  }

  function renderAnalysisJobProgress(panel, job, events) {
    const stateName = cleanText(job.state) || "queued";
    const messages = {
      queued: "等待分析服务开始",
      leased: "正在准备分析材料",
      running: cleanText(job.progress_message) || "正在生成分析报告",
      succeeded: "分析版本已完成",
    };
    setAnalysisStatus(panel, messages[stateName] || `任务状态：${stateName}`, "loading");
    const wrapper = analysisMessage(
      "深度分析运行中",
      messages[stateName] || "正在等待安全执行边界返回结果。",
      true,
    );
    const progress = document.createElement("progress");
    progress.max = Math.max(1, toFiniteNumber(job.progress_total, 2));
    progress.value = clamp(toFiniteNumber(job.progress_current, 0), 0, progress.max);
    progress.setAttribute("aria-label", "分析任务进度");
    wrapper.appendChild(progress);
    const eventList = Array.isArray(events) ? events : [];
    if (eventList.length) {
      const latest = eventList[eventList.length - 1];
      wrapper.appendChild(
        element("small", "", humanize(cleanText(latest.event_type))),
      );
    }
    const cancel = analysisAction("取消任务", async () => {
      try {
        await requestJSON(`${API.jobs}/${encodeURIComponent(job.id)}/cancel`, {
          method: "POST",
          headers: mutationHeaders(),
        });
        cancel.disabled = true;
        cancel.textContent = "已请求取消";
      } catch (error) {
        showToast(`取消失败：${friendlyError(error)}`, true);
      }
    });
    cancel.classList.add("is-compact", "is-danger");
    wrapper.appendChild(cancel);
    panel.body.replaceChildren(wrapper);
  }

  function renderAnalysisLoading(panel, copy) {
    panel.body.replaceChildren(analysisMessage("分析数据载入中", copy, true));
  }

  function renderAnalysisError(panel, title, copy, actions) {
    const wrapper = analysisMessage(title, copy);
    if (actions && actions.retryJob) {
      wrapper.appendChild(
        analysisAction("重试分析任务", () => runAnalysis(panel, actions.retryJob)),
      );
    }
    if (actions && actions.retryHistory) {
      const retry = analysisAction("重新载入历史", () => loadAnalysisHistory(panel, true));
      retry.classList.add("is-secondary");
      wrapper.appendChild(retry);
    }
    panel.body.replaceChildren(wrapper);
  }

  function analysisMessage(title, copy, loading) {
    const wrapper = element("div", `analysis-message${loading ? " is-loading" : ""}`);
    wrapper.append(
      element("strong", "", title),
      element("p", "", copy),
    );
    return wrapper;
  }

  function analysisAction(label, handler) {
    const button = element("button", "analysis-action", label);
    button.type = "button";
    button.addEventListener("click", handler);
    return button;
  }

  function analysisMetric(label, value) {
    const item = element("div", "analysis-metric");
    item.append(element("span", "", label), element("strong", "", value || "—"));
    return item;
  }

  function analysisTextBlock(label, value) {
    const block = element("section", "analysis-text-block");
    block.append(
      element("h4", "", label),
      element("p", "", cleanText(value) || "未提供"),
    );
    return block;
  }

  function appendMetadata(list, label, value) {
    list.append(
      element("dt", "", label),
      element("dd", "", cleanText(value) || "—"),
    );
  }

  function analysisVersionLabel(version, index) {
    const created = parseDate(version && version.created_at);
    const date = created
      ? new Intl.DateTimeFormat("zh-CN", {
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
          hour12: false,
        }).format(created)
      : `版本 ${index + 1}`;
    return `${index === 0 ? "最新 · " : "历史 · "}${date}`;
  }

  function setAnalysisStatus(panel, message, tone) {
    panel.status.textContent = message;
    panel.status.dataset.tone = tone || "";
  }

  async function ensureLocalAccessToken() {
    if (state.accessTokenRequired && !state.accessToken) {
      state.accessToken = window.prompt("请输入本地 API 访问令牌") || null;
      if (!state.accessToken) throw new Error("未提供本地 API 访问令牌");
    }
  }

  function objectValue(value) {
    return value && typeof value === "object" && !Array.isArray(value)
      ? value
      : {};
  }

  function compactValue(value) {
    if (value === null || value === undefined || value === "") return "—";
    if (typeof value === "string") return value;
    if (typeof value === "number" || typeof value === "boolean") return String(value);
    try {
      const text = JSON.stringify(value);
      return text.length > 420 ? `${text.slice(0, 417)}…` : text;
    } catch (_error) {
      return "无法显示";
    }
  }

  function formatDuration(value) {
    const milliseconds = toOptionalNumber(value);
    if (milliseconds === null) return "—";
    if (milliseconds < 1000) return `${formatNumber(milliseconds)} ms`;
    return `${(milliseconds / 1000).toFixed(1)} s`;
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
    if (state.scanning) {
      await cancelActiveJob();
      return;
    }
    state.scanning = true;
    state.cancelRequested = false;
    dom.scanButton.classList.add("is-scanning");
    dom.scanButton.setAttribute("aria-busy", "true");
    const retrying = Boolean(state.retryJobId);
    dom.scanLabel.textContent = retrying ? "正在重新排队…" : "正在提交扫描…";
    dom.scanNote.textContent = "扫描将进入持久任务队列";
    showToast(retrying ? "正在重试扫描任务…" : "正在创建可恢复的扫描任务…");
    let finalNote = "扫描将更新今天的候选池与推荐榜单";

    try {
      if (state.accessTokenRequired && !state.accessToken) {
        state.accessToken = window.prompt("请输入本地 API 访问令牌") || null;
        if (!state.accessToken) throw new Error("未提供本地 API 访问令牌");
      }
      const idempotencyKey = window.crypto && typeof window.crypto.randomUUID === "function"
        ? window.crypto.randomUUID()
        : `web-scan-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      const queued = retrying
        ? await requestJSON(`${API.jobs}/${encodeURIComponent(state.retryJobId)}/retry`, {
            method: "POST",
            headers: mutationHeaders(),
          })
        : await requestJSON(API.scans, {
            method: "POST",
            headers: mutationHeaders({
              "Content-Type": "application/json",
              "Idempotency-Key": idempotencyKey,
            }),
            body: JSON.stringify({}),
          });
      state.retryJobId = null;
      state.activeJobId = queued.id;
      dom.scanLabel.textContent = "取消扫描任务";
      const job = await waitForJob(queued);

      if (job.state !== "succeeded") {
        if (["failed", "cancelled", "timed_out"].includes(job.state)) {
          state.retryJobId = job.id;
        }
        const terminalMessages = {
          cancelled: "扫描任务已取消",
          timed_out: "扫描任务执行超时",
          failed: "扫描任务执行失败",
        };
        throw new Error(
          job.error_message
          || terminalMessages[job.state]
          || `扫描任务已结束（${job.state || "unknown"}）`,
        );
      }

      const result = job.result_data && typeof job.result_data === "object"
        ? job.result_data
        : {};
      const summary = [
        Number.isFinite(Number(result.candidate_count))
          ? `扫描 ${formatNumber(Number(result.candidate_count))} 个候选`
          : "扫描完成",
        Number.isFinite(Number(result.selected_count))
          ? `精选 ${formatNumber(Number(result.selected_count))} 个机会`
          : "",
      ].filter(Boolean).join("，");

      showToast(`${summary}。榜单已更新。`);
      await reloadDaily();
    } catch (error) {
      showToast(`扫描失败：${friendlyError(error)}`, true);
      finalNote = state.retryJobId
        ? "扫描未完成，榜单保留上次成功结果；可重试该任务"
        : "扫描未完成，榜单仍显示上次成功结果";
    } finally {
      state.scanning = false;
      state.activeJobId = null;
      state.cancelRequested = false;
      dom.scanButton.classList.remove("is-scanning");
      dom.scanButton.removeAttribute("aria-busy");
      dom.scanLabel.textContent = state.retryJobId ? "重试上次扫描" : "立即扫描新机会";
      dom.scanNote.textContent = finalNote;
    }
  }

  async function cancelActiveJob() {
    if (!state.activeJobId || state.cancelRequested) return;
    state.cancelRequested = true;
    dom.scanLabel.textContent = "正在请求取消…";
    dom.scanNote.textContent = "后台任务会在当前步骤完成后停止";
    try {
      await requestJSON(
        `${API.jobs}/${encodeURIComponent(state.activeJobId)}/cancel`,
        {
          method: "POST",
          headers: mutationHeaders(),
        },
      );
      showToast("取消请求已记录，等待后台任务确认。 ");
    } catch (error) {
      state.cancelRequested = false;
      showToast(`取消失败：${friendlyError(error)}`, true);
    }
  }

  function mutationHeaders(extra) {
    return {
      ...(extra || {}),
      ...(state.csrfToken ? { "X-CSRF-Token": state.csrfToken } : {}),
      ...(state.accessToken ? { Authorization: `Bearer ${state.accessToken}` } : {}),
    };
  }

  async function waitForTerminalJob(initialJob, options) {
    let job = initialJob;
    const jobTimeoutSeconds = Math.max(
      60,
      toFiniteNumber(job.timeout_seconds, 600),
    );
    // Provider Jobs are processed serially by the least-privileged Provider
    // Worker. A later Top-5 item must therefore include the timeout budgets
    // of the earlier queue positions; otherwise the browser reports a false
    // "still running" error while the durable Job is merely waiting.
    const queuePosition = Math.max(
      0,
      Math.floor(toFiniteNumber(objectValue(options).queuePosition, 0)),
    );
    const deadline = Date.now()
      + (jobTimeoutSeconds * (queuePosition + 1) + 90) * 1000;
    const terminal = new Set(["succeeded", "failed", "cancelled", "timed_out"]);
    const onUpdate = typeof objectValue(options).onUpdate === "function"
      ? objectValue(options).onUpdate
      : null;

    if (onUpdate) onUpdate(job);

    while (!terminal.has(String(job.state || "").toLowerCase())) {
      if (Date.now() >= deadline) {
        throw new Error("任务仍未结束，进度已保留，可稍后继续查看");
      }
      await sleep(1000);
      job = await requestJSON(`${API.jobs}/${encodeURIComponent(job.id)}`);
      if (onUpdate) onUpdate(job);
    }
    return job;
  }

  async function waitForJob(initialJob) {
    let job = initialJob;
    const timeoutSeconds = Math.max(60, toFiniteNumber(job.timeout_seconds, 600) + 90);
    const deadline = Date.now() + timeoutSeconds * 1000;
    const terminal = new Set(["succeeded", "failed", "cancelled", "timed_out"]);

    while (!terminal.has(String(job.state || "").toLowerCase())) {
      const jobState = String(job.state || "queued").toLowerCase();
      if (jobState === "queued") {
        dom.scanNote.textContent = "等待后台扫描服务响应";
      } else if (jobState === "leased") {
        dom.scanNote.textContent = "正在准备 GitHub 扫描";
      } else {
        dom.scanNote.textContent = cleanText(job.progress_message) || "正在检索并评估候选机会";
      }
      if (Date.now() >= deadline) {
        throw new Error("扫描仍未结束，进度已保留，可稍后继续查看");
      }
      await sleep(1000);
      job = await requestJSON(`${API.jobs}/${encodeURIComponent(job.id)}`);
    }
    return job;
  }

  async function reloadDaily() {
    try {
      const [daily, recommendations, shortlist, changes, notifications, dashboard] = await Promise.all([
        requestJSON(API.daily),
        requestJSON(recommendationEndpoint()),
        requestJSON(API.shortlist),
        requestJSON(API.scanChanges),
        requestJSON(API.notifications),
        requestJSON(API.dashboard),
      ]);
      renderDaily(daily);
      renderRecommendations(recommendations);
      renderShortlist(shortlist);
      renderScanChanges(changes);
      renderNotifications(notifications);
      renderContributionDashboard(dashboard);
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

  function formatCurrency(value) {
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: "USD",
      maximumFractionDigits: Number(value) % 1 ? 2 : 0,
    }).format(toFiniteNumber(value, 0));
  }

  function formatRelativeDate(value) {
    const date = parseDate(value);
    if (!date) return "时间未知";
    const minutes = Math.round((date.getTime() - Date.now()) / 60_000);
    const formatter = new Intl.RelativeTimeFormat("zh-CN", { numeric: "auto" });
    if (Math.abs(minutes) < 60) return formatter.format(minutes, "minute");
    const hours = Math.round(minutes / 60);
    if (Math.abs(hours) < 24) return formatter.format(hours, "hour");
    return formatter.format(Math.round(hours / 24), "day");
  }

  function toLocalInputValue(date) {
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
    return local.toISOString().slice(0, 16);
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
