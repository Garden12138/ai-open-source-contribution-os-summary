import { agentLogin, api, el, element, isolationLabel } from './api.js';

const dialog = el('task-dialog');
const form = el('task-form');
const issue = el('issue-url');
const agentSelect = el('agent');
const confirmTrust = el('confirm-trust');
const trustOwner = el('trust-owner');
const create = el('create-task');
let config;
let agents = [];
let previousRepo;
let loading = false;
let submitting = false;
let onCreated;

function repository() {
  try {
    const url = new URL(issue.value);
    if (url.origin === 'https://github.com' && !url.username && !url.password && !url.search) {
      const match = url.pathname.match(/^\/([\w.-]+\/[\w.-]+)\/issues\/[1-9][0-9]*\/?$/);
      return match?.[1].toLowerCase();
    }
  } catch {
    // The URL can be incomplete while the user is typing.
  }
}

function formStatus(message = '') {
  el('form-status').textContent = message;
  el('form-status').hidden = !message;
}

function selectedIsolation() {
  return form.querySelector('[name=isolation]:checked')?.value;
}

function update() {
  if (!config) return;
  const repo = repository();
  const trusted = config.trusted_owners.some(owner => owner.toLowerCase() === repo?.split('/')[0]);
  const isolation = selectedIsolation();
  if (previousRepo !== repo || isolation !== 'host') {
    confirmTrust.checked = trustOwner.checked = false;
  }
  previousRepo = repo;
  el('host-warning').hidden = isolation !== 'host';
  confirmTrust.required = isolation === 'host';
  trustOwner.disabled = !confirmTrust.checked || trusted;
  if (trustOwner.disabled) trustOwner.checked = false;
  const usable = agent => agent.available_isolation.includes(isolation) &&
    (isolation !== 'host' || (agent.installed && ['ok', 'unknown'].includes(agent.status)));
  const current = agentSelect.value;
  agentSelect.replaceChildren(new Option('请选择 Agent', ''));
  for (const agent of agents) {
    const status = agent.installed ? agentLogin(agent.login) : '未安装';
    const option = new Option(`${agent.name} · ${status}`, agent.id);
    option.disabled = !usable(agent);
    agentSelect.append(option);
  }
  agentSelect.value = [current, config.default_agent, agents.find(usable)?.id]
    .find(id => agents.some(agent => agent.id === id && usable(agent))) || '';
  const hint = el('agent-hint');
  hint.replaceChildren();
  for (const agent of agents) {
    if (agent.id !== agentSelect.value && usable(agent) && agent.login === 'logged_in') continue;
    const detail = [];
    if (!agent.installed || agent.status === 'error') detail.push(agent.detail);
    if (!agent.available_isolation.includes(isolation)) detail.push('当前隔离级别不可用。');
    if (agent.login !== 'logged_in' && agent.login_hint) detail.push(`登录指引：${agent.login_hint}`);
    if (agent.isolation_warning && agent.id === agentSelect.value) detail.push(agent.isolation_warning);
    if (detail.length) hint.append(element('p', '', `${agent.name} · ${detail.join(' ')}`));
  }
  create.disabled = loading || submitting || !repo || !agentSelect.value ||
    (isolation === 'host' && !confirmTrust.checked);
}

async function load() {
  loading = true;
  config = null;
  create.disabled = true;
  agentSelect.replaceChildren(new Option('正在检查 Agent…', ''));
  el('agent-hint').replaceChildren();
  el('isolation-options').replaceChildren();
  el('isolation-hint').hidden = el('host-warning').hidden = el('retry-form').hidden = true;
  formStatus();
  try {
    [config, { agents }] = await Promise.all([api('/settings'), api('/agents')]);
    const options = el('isolation-options');
    const hint = el('isolation-hint');
    const defaultSandbox = config.sandbox_backend;
    for (const kind of [defaultSandbox, ...['sbx', 'container'].filter(value =>
      value !== defaultSandbox && config.available_isolation.includes(value)), 'host']) {
      const input = element('input');
      input.type = 'radio';
      input.name = 'isolation';
      input.value = kind;
      input.checked = kind === defaultSandbox;
      const label = element('label', 'choice');
      label.append(input, isolationLabel(kind));
      options.append(label);
    }
    hint.textContent = config.available_isolation.includes(defaultSandbox)
      ? '陌生仓库默认在沙箱内运行。'
      : '当前沙箱后端不可用。请在设置中查看状态，或为可信仓库明确选择本机直跑。';
    hint.hidden = false;
  } catch (error) {
    formStatus(error.message);
    el('retry-form').hidden = false;
  } finally {
    loading = false;
    update();
  }
}

export function bindNewTask(callback) {
  onCreated = callback;
  el('new-task').addEventListener('click', () => {
    form.reset();
    previousRepo = undefined;
    dialog.showModal();
    if (!loading) load();
  });
  el('close-task').addEventListener('click', () => dialog.close());
  el('retry-form').addEventListener('click', load);
  form.addEventListener('input', update);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (create.disabled) return;
    submitting = true;
    update();
    formStatus();
    try {
      const { task_id: id } = await api('/tasks', { method: 'POST', body: {
        issue_url: issue.value, agent: agentSelect.value, isolation: selectedIsolation(),
        trust_confirmed: confirmTrust.checked, trust_owner: !trustOwner.disabled && trustOwner.checked,
      } });
      dialog.close();
      onCreated(id);
    } catch (error) {
      formStatus(error.message);
    } finally {
      submitting = false;
      update();
    }
  });
}
