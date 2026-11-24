import { api, element } from './api.js';
import { parseDiff } from './diff.js';
import { externalURL, renderMarkdown } from './markdown.js';
import { mountPublish } from './publish.js';
import { mountReview } from './review.js';

function externalLink(value, label) {
  const href = externalURL(value);
  if (!href) return element('span', '', label);
  const link = element('a', '', label);
  link.href = href;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  return link;
}

function markdown(text) {
  const body = element('div', 'markdown');
  body.innerHTML = renderMarkdown(text);
  return body;
}

function renderIssue(task, root, refresh, feedback) {
  const snapshot = task?.issue_snapshot;
  if (!snapshot?.title) {
    root.append(element('p', 'muted', 'Issue 快照尚未就绪。'));
    return;
  }
  const issuePath = task.repo_full_name.split('/').map(encodeURIComponent).join('/');
  const url = `https://github.com/${issuePath}/issues/${task.issue_number}`;
  const controls = element('div', 'detail-controls');
  const refreshButton = element('button', '', '刷新 Issue');
  refreshButton.type = 'button';
  refreshButton.disabled = feedback?.busy === true;
  refreshButton.addEventListener('click', refresh);
  controls.append(externalLink(url, `GitHub #${task.issue_number}`), refreshButton);
  root.append(controls);
  if (feedback?.message) {
    const message = element('p', feedback.busy ? 'muted' : 'error', feedback.message);
    message.setAttribute('role', 'status');
    root.append(message);
  }
  root.append(element('h3', '', snapshot.title));
  const state = { open: '开放', closed: '已关闭' }[snapshot.state];
  if (state) root.append(element('p', 'muted', state));
  if (snapshot.assignees?.length) root.append(element('p', 'muted', `已分配给 ${snapshot.assignees.join('、')}`));
  root.append(snapshot.body ? markdown(snapshot.body) : element('p', 'muted', '暂无 Issue 正文。'));
  if (snapshot.comments?.length) {
    const comments = element('details');
    comments.append(element('summary', '', `评论（${snapshot.comments.length}）`));
    for (const comment of snapshot.comments) comments.append(markdown(comment.body || ''));
    root.append(comments);
  }
  if (snapshot.linked_prs?.length) {
    const links = element('ul');
    for (const pr of snapshot.linked_prs) {
      const item = element('li');
      item.append(externalLink(pr.html_url, pr.title || `PR #${pr.number}`));
      links.append(item);
    }
    root.append(element('h3', '', '关联 PR'), links);
  }
}

function renderUsage(task, root) {
  const format = new Intl.NumberFormat('zh-CN');
  const duration = task?.turns.reduce((total, turn) => {
    if (!turn.started_at) return total;
    const stop = turn.completed_at ? Date.parse(turn.completed_at) : Date.now();
    return total + Math.max(0, stop - Date.parse(turn.started_at));
  }, 0);
  const token = count => count == null ? '未知' : format.format(count);
  const seconds = Math.floor((duration || 0) / 1000);
  const time = seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  const data = [
    ['输入 Token', token(task?.tokens_input)],
    ['输出 Token', token(task?.tokens_output)],
    ['执行耗时', time],
    ['已开始轮次', format.format(task?.turns.filter(turn => turn.started_at).length || 0)],
    ['重试', format.format(task?.turns.reduce((count, turn) => count + Number(turn.kind === 'auto_retry'), 0) || 0)],
    ['中断', format.format(task?.turns.filter(turn => turn.status === 'interrupted').length || 0)],
  ];
  const list = element('dl', 'usage');
  for (const [term, value] of data) list.append(element('dt', '', term), element('dd', '', value));
  root.append(list);
}

export function mountDetails(root, path, refreshTask) {
  const tabs = element('nav', 'detail-tabs');
  tabs.setAttribute('aria-label', '任务详情视图');
  const body = element('div', 'detail-body');
  root.replaceChildren(tabs, body);
  root.hidden = false;
  let tab = 'diff';
  let task, files, failure, feedback;
  let closed = false;
  let request = 0;
  let diffTimer;
  let fileIndex = 0;
  const publishRoot = element('div');
  const publish = mountPublish(publishRoot, path);
  const reviewRoot = element('div');
  const review = mountReview(reviewRoot, path);

  const navigation = [
    ['diff', '改动'], ['issue', 'Issue'], ['review', '审查'], ['publish', '发布'], ['usage', '用量'],
  ].map(([key, name]) => {
    const button = element('button', '', name);
    button.type = 'button';
    tabs.append(button);
    button.addEventListener('click', () => {
      tab = key;
      paint();
      if (tab === 'publish') publish.refresh();
      if (tab === 'review') review.refresh();
    });
    return [key, button];
  });

  function renderDiff() {
    if (failure || !files?.length) {
      body.append(element('p', failure ? 'error' : 'muted', failure || (files ? '工作区暂无改动。' : '正在加载改动…')));
      if (failure) {
        const retry = element('button', '', '重新加载');
        retry.addEventListener('click', loadDiff);
        body.append(retry);
      }
      return;
    }
    const chooser = element('select', 'diff-files');
    chooser.setAttribute('aria-label', '已改动文件');
    for (const [index, file] of files.entries()) {
      chooser.append(new Option(`${file.path || '文件'}  +${file.additions} −${file.deletions}`, index));
    }
    chooser.value = fileIndex;
    const patch = element('pre', 'diff-patch');
    const renderPatch = () => {
      const content = document.createDocumentFragment();
      for (const line of files[fileIndex].lines) content.append(element('span', `diff-${line.kind}`, line.text));
      patch.replaceChildren(content);
    };
    chooser.addEventListener('change', () => {
      fileIndex = Number(chooser.value);
      renderPatch();
    });
    renderPatch();
    body.append(chooser, patch);
  }

  function paint() {
    for (const [key, button] of navigation) button.setAttribute('aria-pressed', String(key === tab));
    body.replaceChildren();
    if (tab === 'diff') renderDiff();
    if (tab === 'issue') renderIssue(task, body, refreshIssue, feedback);
    if (tab === 'usage') renderUsage(task, body);
    if (tab === 'publish') body.append(publishRoot);
    if (tab === 'review') body.append(reviewRoot);
  }

  async function loadDiff() {
    const current = ++request;
    try {
      const result = await api(`${path}/diff`);
      if (closed || current !== request) return;
      const selected = files?.[fileIndex]?.path;
      files = parseDiff(result.diff_text, result.files);
      fileIndex = Math.max(0, files.findIndex(file => file.path === selected));
      failure = undefined;
    } catch (error) {
      if (closed || current !== request) return;
      failure = error.message;
    }
    if (tab === 'diff') paint();
  }

  async function refreshIssue() {
    feedback = { busy: true, message: '正在刷新 Issue…' };
    paint();
    try {
      await api(`${path}/issue/refresh`, { method: 'POST' });
    } catch (error) {
      feedback = { message: error.message };
      if (tab === 'issue' && !closed) paint();
    }
  }

  paint();
  loadDiff();
  return {
    update(next) {
      const previous = task?.issue_snapshot;
      task = next;
      publish.update(next);
      review.update(next);
      if (tab === 'usage' || (tab === 'issue' && JSON.stringify(previous) !== JSON.stringify(next.issue_snapshot))) paint();
    },
    event(event) {
      if (event.type.startsWith('review.')) review.event();
      if (event.type.startsWith('publish.')) {
        publish.event();
        if (event.type === 'publish.succeeded') refreshTask();
      }
      if (event.type === 'diff.updated') {
        clearTimeout(diffTimer);
        diffTimer = setTimeout(loadDiff, 120);
      }
      if (event.type === 'issue.updated' || event.type === 'issue.refresh_failed') {
        feedback = event.type === 'issue.refresh_failed' ? { message: event.payload.message } : undefined;
        if (tab === 'issue') paint();
        if (event.type === 'issue.updated') refreshTask();
      }
    },
    close() {
      publish.close();
      review.close();
      closed = root.hidden = true;
      clearTimeout(diffTimer);
      root.replaceChildren();
    },
  };
}
