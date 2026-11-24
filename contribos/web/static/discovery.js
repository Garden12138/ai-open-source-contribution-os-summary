import { api, el, element } from './api.js';

const buckets = { bounty: '赏金', high_impact: '高影响', tech_match: '技术匹配', strategic: '战略', best_available: '综合推荐' };
const dimensions = { reward_reliability: '赏金可信度', acceptance_probability: '接受概率', tech_match: '技术匹配',
  project_impact: '项目影响力', issue_clarity: '描述清晰度', competition: '竞争机会', learning_value: '学习价值' };
const risks = { issue_has_linked_pr: '已有 PR 参与此 Issue', scope_may_be_too_large: '改动范围可能较大', high_discussion_or_competition: '讨论较多或竞争较高',
  issue_is_very_old: 'Issue 创建时间较早', repository_language_unknown: '仓库语言未知',
  bounty_amount_or_terms_unclear: '赏金金额或条款不明确' };

export function listQuery(view, filters, offset) {
  const query = new URLSearchParams({ view, offset, limit: 30 });
  if (view !== 'daily') {
    for (const [key, value] of filters) if (value.trim()) query.set(key, value.trim());
  }
  return query;
}

function opportunityCard(item, openTask, mark) {
  const card = element('article', 'opportunity');
  const heading = element('div', 'section-title');
  const repo = item.repository;
  heading.append(element('span', 'muted', `${item.repo_full_name} #${item.issue_number}`),
    element('strong', 'badge', `${item.score_total} 分`));
  card.append(heading, element('h2', '', item.title));
  const tags = [buckets[item.bucket], repo.language, `★ ${repo.stars || 0}`, repo.license_spdx,
    ...item.labels, item.score.bounty_amount_usd ? `$${item.score.bounty_amount_usd}` : ''].filter(Boolean);
  card.append(element('p', 'muted', tags.join(' · ')));
  const linked = item.has_linked_pr == null ? '关联 PR 未知' : item.has_linked_pr ? '已有关联 PR' : '未发现关联 PR';
  const outcomes = repo.external_prs_closed == null ? '外部 PR 数据未知' : repo.external_prs_closed === 0
    ? '近 90 天无已关闭的外部 PR' : `近 90 天已关闭的外部 PR 中，${repo.external_prs_merged}/${repo.external_prs_closed} 已合并`;
  card.append(element('p', 'muted', `${linked} · ${outcomes}`));
  const breakdown = element('details');
  breakdown.append(element('summary', '', '评分明细'));
  const dl = element('dl', 'score-breakdown');
  for (const [name, score] of Object.entries(item.score.components || {})) {
    dl.append(element('dt', '', dimensions[name] || name), element('dd', '', score));
  }
  breakdown.append(dl);
  const reasons = item.score.risk_reasons || [];
  card.append(breakdown, element('p', reasons.length ? 'risk' : 'muted',
    reasons.length ? reasons.map(r => risks[r] || r).join(' · ') : '未发现评分风险'));
  const actions = element('div', 'card-actions');
  const start = element('button', 'primary', '开始贡献');
  start.onclick = () => openTask(item);
  actions.append(start);
  for (const [value, label, undo] of [['starred', '收藏', '取消收藏'], ['ignored', '忽略', '恢复']]) {
    const button = element('button', '', item.mark === value ? undo : label);
    button.onclick = () => mark(item, item.mark === value ? null : value, button);
    actions.append(button);
  }
  const link = element('a', '', '查看 Issue');
  link.href = item.url;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  actions.append(link);
  card.append(actions);
  return card;
}

export function mountDiscovery(openTask) {
  const controller = new AbortController();
  const { signal } = controller;
  const options = { signal };
  const form = el('opportunity-filters'), list = el('opportunities'), more = el('load-opportunities');
  const status = el('opportunities-status'), scanButton = el('scan-now'), scanStatus = el('scan-status');
  let view = 'daily', offset = 0, version = 0, stream, currentScan, checking = false;

  async function load(append = false) {
    const ticket = ++version;
    if (!append) {
      offset = 0;
      list.replaceChildren();
    }
    more.hidden = true;
    status.textContent = '正在加载…';
    try {
      const query = listQuery(view, new FormData(form), offset);
      const data = await api(`/opportunities?${query}`, options);
      if (ticket !== version || signal.aborted) return;
      list.append(...data.opportunities.map(item => opportunityCard(item, openTask, mark)));
      offset += data.opportunities.length;
      more.hidden = !data.more;
      status.textContent = offset ? '' : view === 'daily' ? '今天尚无推荐。可立即扫描公开 Issue。' : '没有符合条件的机会。';
    } catch (error) {
      if (ticket === version && !signal.aborted) status.textContent = error.message;
    }
  }

  async function mark(item, value, button) {
    button.disabled = true;
    try {
      await api(`/opportunities/${encodeURIComponent(item.id)}/mark`, { ...options, method: 'PUT', body: { mark: value } });
      if (!signal.aborted) await load();
    } catch (error) {
      if (!signal.aborted) status.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  }

  function showScan(run) {
    const stats = run.stats;
    scanButton.disabled = run.status === 'running';
    if (scanButton.disabled) {
      scanStatus.textContent = `扫描中 · 已搜索 ${stats.queries || 0}/${stats.queries_total || '…'} 组，已评估 ${stats.processed || 0}/${stats.candidates || 0} 个 Issue`;
    } else {
      const failure = run.error_code === 'github_rate_limited' ? 'GitHub 读取限流，请稍后重试或在宿主登录 gh。' : run.error_code;
      scanStatus.textContent = run.status === 'completed' ? `扫描完成 · ${stats.candidates} 个候选，${stats.selected} 个推荐`
        : `扫描${run.status === 'interrupted' ? '已中断' : '失败'} · ${failure || ''}`;
    }
  }

  function watch(run) {
    stream?.close();
    stream = undefined;
    currentScan = run.id;
    showScan(run);
    if (run.status !== 'running') return;
    stream = new EventSource(`/api/scans/${encodeURIComponent(run.id)}/stream`);
    stream.onmessage = event => {
      const update = JSON.parse(event.data);
      showScan(update);
      if (update.status !== 'running') {
        stream.close();
        stream = undefined;
        load();
      }
    };
    stream.onerror = () => { scanStatus.textContent = '正在重新连接扫描进度…'; };
  }

  async function latest() {
    if (checking || stream) return;
    checking = true;
    try {
      const run = await api('/scans/latest', options);
      if (!signal.aborted && !stream && run && run.id !== currentScan) {
        watch(run);
        if (run.status === 'completed') load();
      }
    } catch (error) {
      if (!signal.aborted) scanStatus.textContent = error.message;
    } finally {
      checking = false;
    }
  }

  scanButton.disabled = false;
  scanStatus.textContent = '';
  scanButton.addEventListener('click', async () => {
    scanButton.disabled = true;
    const repository = el('scan-repo').value.trim();
    try {
      const run = await api('/scans', { ...options, method: 'POST', body: repository ? { repository } : undefined });
      if (!signal.aborted) watch(run);
    } catch (error) {
      if (!signal.aborted) {
        scanStatus.textContent = error.message;
        scanButton.disabled = false;
      }
    }
  }, options);

  function select(value) {
    view = value;
    form.hidden = view === 'daily';
    for (const button of el('opportunity-views').children) button.setAttribute('aria-pressed', String(button.dataset.view === view));
    load();
  }
  el('opportunity-views').addEventListener('click', event => {
    if (event.target.dataset.view) select(event.target.dataset.view);
  }, options);
  form.addEventListener('submit', event => { event.preventDefault(); load(); }, options);
  more.addEventListener('click', () => load(true), options);
  select('daily');
  latest();
  const timer = setInterval(latest, 15000);
  return () => { controller.abort(); stream?.close(); clearInterval(timer); };
}
