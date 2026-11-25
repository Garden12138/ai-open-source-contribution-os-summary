import { api, element } from './api.js';
import { externalURL } from './markdown.js';

const warnings = {
  binary_files: '改动含二进制文件。',
  workflow_changes: '改动含 GitHub Actions 工作流，推送需要 workflow 权限。',
  large_diff: '改动规模较大，请核对完整 diff。',
  draft_template: 'Agent 未返回有效的 PR 文案，已使用模板。',
};

const confirmLabels = {
  create_pr: '确认发布 Draft PR', push_followup: '确认追加提交',
  update_pr: '确认更新 PR 文案', mark_ready: '确认标记 Ready for review',
};

export function publicationActions(status, busy, dirty) {
  return {
    prepare: !busy && !['confirmed', 'running', 'unknown', 'succeeded'].includes(status),
    confirm: !busy && !dirty && status === 'previewed',
    reconcile: !busy && status === 'unknown',
  };
}

function previewDetails(preview) {
  const root = element('div', 'publication-preview');
  const details = element('dl', 'usage');
  const add = (label, value) => details.append(element('dt', '', label), element('dd', '', value));
  add('PR 目标分支', `${preview.repo} · ${preview.default_branch}`);
  add('PR 来源分支', `${preview.fork} · ${preview.branch}`);
  if (preview.number) add('PR', `#${preview.number}`);
  if (preview.kind === 'mark_ready' || preview.kind === 'update_pr') {
    add('当前提交', preview.commit_sha);
    if (preview.kind === 'mark_ready') add('状态变更', 'Draft → Ready for review');
    root.append(details, element('p', '', preview.current_pr.title));
    if (preview.kind === 'update_pr') {
      root.append(element('pre', '', preview.current_pr.body));
      if (preview.title !== preview.current_pr.title || preview.body !== preview.current_pr.body) {
        root.append(element('h4', '', '预览文案'), element('p', '', preview.title), element('pre', '', preview.body));
      }
    }
    return root;
  }
  add('基线', preview.base_sha);
  if (preview.parent_sha) add('已发布的父提交', preview.parent_sha);
  add('上游 HEAD', preview.upstream_sha);
  add('落后上游', `${preview.behind} 个提交`);
  add('提交', preview.commit_sha);
  add('作者', `${preview.author.name} <${preview.author.email}>`);
  add('Issue', ({ open: '开放', closed: '已关闭' })[preview.checks.issue_state] || '未知');
  add('新增关联 PR', String(preview.checks.new_linked_prs.length));
  const review = preview.checks.review;
  const reviewVerdict = { approve: '通过', request_changes: '建议修改' };
  add('审查', review?.current ? (reviewVerdict[review.verdict] || '已完成') : (review ? '当前改动未通过有效审查' : '未审查'));
  root.append(details);
  if (review?.current && review.summary) root.append(element('p', '', review.summary));
  if (preview.checks.new_linked_prs.length) {
    const links = element('ul');
    for (const pr of preview.checks.new_linked_prs) {
      const link = element('a', '', pr.title || `PR #${pr.number}`);
      link.href = externalURL(pr.html_url) || '#';
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      const item = element('li');
      item.append(link);
      links.append(item);
    }
    root.append(links);
  }
  for (const warning of preview.warnings) root.append(element('p', 'warning', warnings[warning] || warning));
  const tests = element('details');
  tests.append(element('summary', '', '最近的测试命令'));
  for (const test of preview.checks.tests) {
    tests.append(element('pre', '', `${test.command}\n${test.exit_code == null ? '退出码未上报' : `退出码 ${test.exit_code}`}`));
  }
  if (!preview.checks.tests.length) tests.append(element('p', 'muted', '没有已记录的测试命令。'));
  const files = element('ul');
  for (const file of preview.files) files.append(element('li', '', `${file.path} · ${file.binary ? '二进制' : `+${file.additions} −${file.deletions}`}`));
  const patch = element('details');
  patch.append(element('summary', '', `完整 diff · ${preview.files.length} 个文件`), files,
    element('pre', 'publish-diff', preview.diff));
  root.append(tests, patch, element('p', 'muted', `提交信息 · ${preview.commit_message}`));
  return root;
}

export function mountPublish(root, path) {
  const controller = new AbortController();
  const endpoint = `${path}/publish`;
  let view, task, draft, error, timer;
  let pending = false;
  let revision = 0;

  function button(label, action, css = '') {
    const node = element('button', css, label);
    node.type = 'button';
    node.addEventListener('click', action);
    return node;
  }

  async function refresh() {
    const version = ++revision;
    try {
      const result = await api(endpoint, { signal: controller.signal });
      if (version !== revision || controller.signal.aborted) return;
      view = result;
    } catch (failure) {
      if (version !== revision || controller.signal.aborted) return;
      error = failure.message;
    }
    paint();
  }

  async function action(suffix, method, body) {
    if (pending) return;
    pending = true;
    error = undefined;
    paint();
    try {
      await api(endpoint + suffix, { method, body, signal: controller.signal });
    } catch (failure) {
      error = failure.message;
    } finally {
      pending = false;
      if (!controller.signal.aborted) await refresh();
    }
  }

  function paint() {
    const pub = view?.publication;
    const status = pub?.status;
    const busy = pending || view?.preparing || ['queued', 'running', 'waiting_retry'].includes(task?.agent_state);
    root.replaceChildren();
    const notice = error || view?.error?.message || pub?.message;
    if (notice) {
      const feedback = element('p', 'error', notice);
      feedback.setAttribute('role', 'status');
      root.append(feedback);
    }
    if (view?.preparing || ['confirmed', 'running'].includes(status)) {
      const progress = element('p', 'muted', view?.preparing ? '正在准备发布预览…' : '正在执行已确认的操作…');
      progress.setAttribute('role', 'status');
      root.append(progress);
    }
    if (!view) return;
    if (publicationActions(status, busy, false).prepare && task?.status === 'ready') {
      root.append(button(pub ? '重新准备 PR' : '准备 PR', () => action('/preview', 'POST')));
    }
    if (view.pull) {
      const link = element('a', '', `打开 PR #${view.pull.number}`);
      link.href = externalURL(view.pull.url) || '#';
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      root.append(link);
      if (view.pull.state === 'open' && !busy && !['unknown', 'confirmed', 'running'].includes(status)) {
        const actions = element('div', 'detail-controls');
        const prepare = (label, kind) => button(label, () => action('/preview', 'POST', { kind }));
        actions.append(prepare('预览追加提交', 'push_followup'), prepare('编辑 PR 文案', 'update_pr'));
        if (view.pull.draft) actions.append(prepare('预览 Ready for review', 'mark_ready'));
        root.append(actions);
      }
    }
    if (!pub) return;
    if (status === 'unknown') {
      root.append(element('p', 'warning', '发布结果待核实。对账会查询并继续原先确认的发布。'));
      const reconcile = button('对账并继续', () => action('/reconcile', 'POST', { preview_hash: pub.preview_hash }));
      reconcile.disabled = !publicationActions(status, busy, false).reconcile;
      root.append(reconcile);
    }
    root.append(previewDetails(pub.preview));
    if (status === 'previewed') {
      if (['create_pr', 'update_pr'].includes(pub.kind)) renderEditor(pub, busy);
      else {
        const confirm = button(confirmLabels[pub.kind], () => action('/confirm', 'POST', { preview_hash: pub.preview_hash }), 'primary');
        confirm.disabled = busy;
        root.append(confirm);
      }
    }
  }

  function renderEditor(pub, busy) {
    if (draft?.id !== pub.id) draft = { id: pub.id, title: pub.preview.title, body: pub.preview.body };
    const form = element('div', 'publish-editor');
    const title = element('input');
    Object.assign(title, { value: draft.title, maxLength: 256, disabled: busy === true });
    const body = element('textarea');
    Object.assign(body, { value: draft.body, maxLength: 65536, rows: 8, disabled: busy === true });
    for (const [label, field] of [['PR 标题', title], ['PR 正文', body]]) {
      const wrapper = element('label', '', label);
      wrapper.append(field);
      form.append(wrapper);
    }
    const save = button('保存文案', () => action(`/preview/${encodeURIComponent(pub.id)}`, 'PATCH', { title: draft.title, body: draft.body }));
    const confirm = button(confirmLabels[pub.kind], () => action('/confirm', 'POST', { preview_hash: pub.preview_hash }), 'primary');
    function edit() {
      draft = { id: pub.id, title: title.value, body: body.value };
      const dirty = draft.title !== pub.preview.title || draft.body !== pub.preview.body;
      save.disabled = busy || !dirty || !draft.title.trim();
      confirm.disabled = !publicationActions(pub.status, busy, dirty).confirm;
      if (pub.kind === 'update_pr' && draft.title === pub.preview.current_pr.title && draft.body === pub.preview.current_pr.body) {
        confirm.disabled = true;
      }
    }
    edit();
    title.addEventListener('input', edit);
    body.addEventListener('input', edit);
    const actions = element('div', 'detail-controls');
    actions.append(save, confirm);
    form.append(actions);
    root.append(form);
  }

  return {
    refresh,
    update(next) {
      const changed = task?.status !== next.status || task?.agent_state !== next.agent_state;
      task = next;
      if (changed) paint();
    },
    event() {
      clearTimeout(timer);
      timer = setTimeout(refresh, 100);
    },
    close() {
      controller.abort();
      clearTimeout(timer);
    },
  };
}
