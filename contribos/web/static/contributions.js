import { api, element, taskPath } from './api.js';
import { externalURL } from './markdown.js';

const labels = { OPEN: '开放', CLOSED: '已关闭', DRAFT: '草稿', MERGED: '已合并', APPROVED: '已批准',
  REVIEW_REQUIRED: '待审查', CHANGES_REQUESTED: '要求修改', PENDING: '运行中', EXPECTED: '等待运行',
  SUCCESS: '通过', FAILURE: '失败', ERROR: '错误' };

export function prStatus(pr) {
  if (pr.merged) return labels.MERGED;
  if (pr.state === 'closed') return labels.CLOSED;
  return pr.draft ? labels.DRAFT : labels.OPEN;
}

export function calendar(year) {
  const first = Date.UTC(year, 0, 1);
  const padding = (new Date(first).getUTCDay() + 6) % 7;
  const dates = Array(padding).fill(null);
  for (let date = first; date < Date.UTC(year + 1, 0, 1); date += 86400000) {
    dates.push(new Date(date).toISOString().slice(0, 10));
  }
  return dates;
}

function activity(days, year, onYear) {
  const box = element('section', 'settings-group');
  const title = element('div', 'section-title');
  const select = element('select');
  select.setAttribute('aria-label', '活动年份');
  const years = new Set([year, new Date().getUTCFullYear(), ...Object.keys(days).map(day => +day.slice(0, 4))]);
  for (const y of [...years].sort((a, b) => b - a)) select.add(new Option(y, y, false, y === year));
  select.onchange = () => onYear(+select.value);
  title.append(element('h2', '', '每日提交'), select);
  const scroll = element('div', 'calendar-scroll');
  const grid = element('div', 'contribution-calendar');
  for (const day of calendar(year)) {
    const count = days[day] || 0;
    const cell = element('span', day ? 'day level-' + Math.min(count, 3) : 'empty-day');
    if (day) {
      cell.title = day + ' · ' + count + ' 个 PR';
      cell.setAttribute('aria-label', cell.title);
      cell.setAttribute('role', 'img');
    }
    grid.append(cell);
  }
  scroll.append(grid);
  box.append(title, scroll, element('p', 'muted', '每列一周，从周一到周日。颜色越深，当天提交的 PR 越多（UTC）。'));
  return box;
}

function monthly(months) {
  const section = element('section', 'settings-group');
  const table = element('table', 'contribution-months');
  const head = table.createTHead().insertRow();
  for (const label of ['月份（UTC）', '已提交', '已合并']) head.append(element('th', '', label));
  const body = table.createTBody();
  for (const [month, value] of Object.entries(months).sort(([a], [b]) => b.localeCompare(a))) {
    const row = body.insertRow();
    for (const text of [month, value.submitted, value.merged]) row.insertCell().textContent = text;
  }
  section.append(element('h2', '', '月度统计'), table);
  return section;
}

function distribution(title, counts) {
  const section = element('section', 'settings-group');
  const values = element('dl', 'distribution');
  for (const [name, count] of Object.entries(counts).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))) {
    values.append(element('dt', '', name === 'Unknown' ? '未知' : name), element('dd', '', count));
  }
  section.append(element('h2', '', title), values);
  return section;
}

function record(pr, refresh) {
  const item = element('article', 'contribution-record');
  const title = element('h3');
  const link = element('a', '', pr.title || pr.repo_full_name + ' #' + pr.number);
  const url = externalURL(pr.url);
  if (url) Object.assign(link, { href: url, target: '_blank', rel: 'noopener noreferrer' });
  title.append(link);
  const repo = element('p', 'muted', pr.repo_full_name + ' #' + pr.number);
  const badges = element('div', 'card-actions');
  badges.append(element('span', 'badge', prStatus(pr)));
  if (pr.has_bounty) badges.append(element('span', 'badge warning', '赏金'));
  const detail = element('p', 'muted', [pr.language, pr.created_at?.slice(0, 10), pr.comments_count + ' 条评论',
    pr.review_decision && ((pr.review_source === 'reviews' ? '审查意见' : '审查结论') + ' · ' +
      (labels[pr.review_decision] || pr.review_decision)), pr.checks_state && ('CI · ' + (labels[pr.checks_state] || pr.checks_state))]
    .filter(Boolean).join(' · '));
  const actions = element('div', 'card-actions');
  if (pr.task_id) {
    const task = element('a', '', '打开任务');
    task.href = '#task/' + encodeURIComponent(pr.task_id);
    actions.append(task);
  }
  const button = element('button', '', pr.refreshing ? '正在刷新…' : '刷新状态');
  button.disabled = pr.refreshing;
  button.onclick = () => refresh(pr, button);
  actions.append(button);
  item.append(repo, title, badges, detail, actions);
  if (pr.error) item.append(element('p', 'error', pr.error));
  return item;
}

export function mountContributions(root) {
  const controller = new AbortController();
  let year = new Date().getUTCFullYear();
  let timer;
  let version = 0;
  root.replaceChildren(element('p', 'muted', '正在加载贡献记录…'));

  async function refresh(pr, button) {
    button.disabled = true;
    try {
      const path = pr.task_id ? taskPath(pr.task_id) + '/pr/refresh' : '/pull-requests/' + pr.id + '/refresh';
      await api(path, { method: 'POST', signal: controller.signal });
      await load();
    } catch (error) {
      if (controller.signal.aborted) return;
      button.after(element('span', 'error', error.message));
      button.disabled = false;
    }
  }

  function render(stats) {
    if (!stats.pull_requests.length) {
      root.replaceChildren(element('p', 'muted', '暂无已发布的 PR。'));
      return;
    }
    const list = element('section', 'settings-group');
    list.append(element('h2', '', '已发布的 PR'), ...stats.pull_requests.map(pr => record(pr, refresh)));
    const groups = element('div', 'contribution-groups');
    groups.append(monthly(stats.months), distribution('仓库分布', stats.repositories), distribution('语言分布', stats.languages));
    root.replaceChildren(activity(stats.days, year, value => { year = value; render(stats); }), groups, list);
  }

  async function load() {
    clearTimeout(timer);
    const request = ++version;
    let delay = 60000;
    try {
      const stats = await api('/stats', { signal: controller.signal });
      if (request !== version || controller.signal.aborted) return;
      render(stats);
      if (stats.pull_requests.some(pr => pr.refreshing)) delay = 1200;
    } catch (error) {
      if (!controller.signal.aborted) root.replaceChildren(element('p', 'error', error.message));
    } finally {
      if (!controller.signal.aborted && version === request) timer = setTimeout(load, delay);
    }
  }
  load();
  return () => { clearTimeout(timer); controller.abort(); };
}
