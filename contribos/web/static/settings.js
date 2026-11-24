import { agentLogin, api, element, isolationLabel } from './api.js';

const notificationNames = {
  turn_completed: '一轮结束',
  request_waiting: '权限请求或提问',
  turn_error: '出错停下',
  publish_result: '发布结果',
  daily_recommendations: '每日推荐就绪',
};
const checkLabels = { not_run: '未检测', unavailable: '无法检测', ok: '正常', missing: '未安装', not_logged_in: '未登录', error: '检查失败', unknown: '状态未知' };

function select(options, value) {
  const field = element('select');
  for (const [id, name] of options) field.append(new Option(name, id));
  field.value = value ?? '';
  return field;
}

function field(label, input) {
  const wrapper = element('label', 'settings-field');
  wrapper.append(element('span', '', label), input);
  return wrapper;
}

function group(form, title) {
  const section = element('section', 'settings-group');
  section.append(element('h2', '', title));
  form.append(section);
  return section;
}

function numberInput(value, max) {
  const input = element('input');
  Object.assign(input, { type: 'number', value, min: 1, max, step: 1, required: true });
  return input;
}

function preferences(root, config, save) {
  const form = element('form');
  const general = group(form, '任务默认值');
  const profiles = config.agent_profiles.map(profile => [profile.id, profile.name]);
  const defaultAgent = select(profiles, config.default_agent);
  const reviewAgent = select([['', '自动选择'], ...profiles], config.review_agent);
  const policy = select([['plan', '评估并给出方案，等待回复'], ['implement', '直接实现']], config.first_turn_policy);
  general.append(field('默认 Agent', defaultAgent), field('审查 Agent', reviewAgent), field('首轮策略', policy));

  const discovery = group(form, '每日发现');
  const scanTime = element('input');
  Object.assign(scanTime, { type: 'time', required: true, value: config.scan_time });
  const languages = element('input');
  languages.value = config.preferred_languages.join(', ');
  const keywords = element('input');
  keywords.value = config.strategic_keywords.join(', ');
  const terms = input => input.value.split(/[,，]/).map(text => text.trim()).filter(Boolean);
  discovery.append(field('每日扫描时间（本机时间）', scanTime), field('偏好语言（逗号分隔）', languages),
    field('战略关键词（逗号分隔）', keywords));

  const execution = group(form, '隔离与可信 owner');
  const sandbox = select(['sbx', 'container'].map(kind => [
    kind, `${isolationLabel(kind)} · ${config.available_isolation.includes(kind) ? '可用' : '未配置'}`,
  ]), config.sandbox_backend);
  const owners = element('textarea');
  owners.rows = 3;
  owners.value = config.trusted_owners.join('\n');
  const domains = element('textarea');
  domains.rows = 3;
  domains.value = config.sandbox_extra_domains.join('\n');
  execution.append(field('默认沙箱后端', sandbox), field('可信 owner（每行一个）', owners),
    field('沙箱额外放行的 HTTPS 域名（每行一个）', domains),
    element('p', 'muted', '额外域名使用 443 端口，下次访问工作区时生效。使用上游代理时，请在 sbx 的沙箱代理例外中加入这些域名，以便校验解析地址。'));

  const resources = group(form, '资源上限');
  const concurrency = numberInput(config.concurrency, 64);
  const cpus = numberInput(config.resource_limits.cpus, 1024);
  const memory = numberInput(config.resource_limits.memory_mib, 2147483647);
  memory.min = 512;
  const disk = numberInput(config.resource_limits.disk_mib, 2147483647);
  resources.append(field('同时运行的轮次', concurrency),
    element('p', 'muted', '并发设置在后端重启后生效。CPU 和内存上限在创建沙箱时分配。磁盘用量超过上限时暂停任务，调整上限后可继续。'),
    field('沙箱 CPU 核数', cpus), field('沙箱内存（MiB）', memory), field('沙箱磁盘（MiB）', disk));

  const publishing = group(form, 'Git 提交作者');
  const authorName = element('input');
  Object.assign(authorName, { value: config.git_author_name, maxLength: 128 });
  const authorEmail = element('input');
  Object.assign(authorEmail, { value: config.git_author_email, maxLength: 320, type: 'email' });
  const signoff = element('input');
  Object.assign(signoff, { type: 'checkbox', checked: config.git_dco_signoff });
  const signoffLabel = element('label', 'choice');
  signoffLabel.append(signoff, '添加 DCO Signed-off-by');
  publishing.append(field('作者姓名', authorName), field('作者邮箱', authorEmail), signoffLabel);

  const tools = group(form, '命令行工具路径');
  tools.append(element('p', 'muted', '填写可信可执行文件的绝对路径。留空时使用 PATH。'));
  const paths = config.tools.map(name => {
    const input = element('input');
    input.placeholder = '自动解析';
    input.maxLength = 4096;
    input.value = config.tool_paths[name] || '';
    tools.append(field(name, input));
    return [name, input];
  });

  const notifications = group(form, '系统通知');
  const switches = Object.entries(notificationNames).map(([name, title]) => {
    const input = element('input');
    Object.assign(input, { type: 'checkbox', checked: config.notifications[name] });
    const label = element('label', 'choice');
    label.append(input, title);
    notifications.append(label);
    return [name, input];
  });

  const actions = element('div', 'settings-actions');
  const button = element('button', 'primary', '保存设置');
  button.type = 'submit';
  const status = element('p', 'muted');
  status.setAttribute('role', 'status');
  actions.append(button, status);
  form.append(actions);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (button.disabled) return;
    button.disabled = true;
    status.className = 'muted';
    status.textContent = '正在保存…';
    const trusted = owners.value.split(/[\n,]+/).map(owner => owner.trim()).filter(Boolean);
    if (trusted.some(owner => !/^[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?$/i.test(owner))) {
      status.className = 'error';
      status.textContent = '请输入有效的 GitHub owner，每行一个。';
      button.disabled = false;
      return;
    }
    try {
      await api('/settings', { method: 'PUT', body: {
        scan_time: scanTime.value, preferred_languages: terms(languages), strategic_keywords: terms(keywords),
        default_agent: defaultAgent.value,
        review_agent: reviewAgent.value || null,
        first_turn_policy: policy.value,
        sandbox_backend: sandbox.value,
        trusted_owners: trusted,
        sandbox_extra_domains: domains.value.split(/\n/).map(value => value.trim()).filter(Boolean),
        concurrency: concurrency.valueAsNumber,
        resource_limits: { cpus: cpus.valueAsNumber, memory_mib: memory.valueAsNumber, disk_mib: disk.valueAsNumber },
        tool_paths: Object.fromEntries(paths.map(([name, input]) => [name, input.value.trim()])),
        git_author_name: authorName.value.trim(),
        git_author_email: authorEmail.value.trim(),
        git_dco_signoff: signoff.checked,
        notifications: Object.fromEntries(switches.map(([name, input]) => [name, input.checked])),
      } });
      status.textContent = '设置已保存。';
      save();
    } catch (error) {
      status.className = 'error';
      status.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });
  root.replaceChildren(form);
}

function diagnose(panel, label, run, render, signal) {
  const top = element('div', 'section-title');
  const button = element('button', '', '重新检测');
  button.type = 'button';
  top.append(element('h2', '', label), button);
  const body = element('div', 'diagnostic-results');
  panel.append(top, body);

  async function load() {
    button.disabled = true;
    body.replaceChildren(element('p', 'muted', '正在检测…'));
    try {
      const results = await run(signal);
      if (!signal.aborted) render(body, results);
    } catch (error) {
      if (!signal.aborted) body.replaceChildren(element('p', 'error', error.message));
    } finally {
      button.disabled = false;
    }
  }

  button.addEventListener('click', load);
  return load;
}

function setAgents(root, { agents }) {
  root.replaceChildren();
  if (!agents.length) root.append(element('p', 'muted', '暂无 Agent。'));
  for (const agent of agents) {
    const card = element('article', 'diagnostic');
    const state = agent.installed ? agentLogin(agent.login) : '未安装';
    card.append(element('h3', '', `${agent.name} · ${state}`));
    if (agent.version) card.append(element('p', 'muted', `版本 ${agent.version}`));
    card.append(element('p', '', agent.detail));
    if (agent.available_isolation.length) card.append(element('p', 'muted',
      `可用隔离级别：${agent.available_isolation.map(isolationLabel).join('、')}`));
    if (agent.login_hint && agent.login !== 'logged_in') card.append(element('code', 'login-hint', agent.login_hint));
    if (agent.sandbox_unavailable_reason) card.append(element('p', 'muted', agent.sandbox_unavailable_reason));
    if (agent.isolation_warning) card.append(element('p', 'warning', agent.isolation_warning));
    root.append(card);
  }
}

function setChecks(root, { checks }) {
  root.replaceChildren();
  if (!checks.length) root.append(element('p', 'muted', '暂无自检结果。'));
  for (const check of checks) {
    const card = element('div', 'diagnostic');
    card.append(element('strong', '', `${check.name} · ${checkLabels[check.status] || check.status}`),
      element('p', 'muted', check.detail));
    root.append(card);
  }
}

export function mountSettings(root) {
  const controller = new AbortController();
  const options = { signal: controller.signal };
  const form = element('div');
  const agents = element('section', 'settings-group');
  const doctor = element('section', 'settings-group');
  root.replaceChildren(form, agents, doctor);

  const loadAgents = diagnose(agents, 'Agent 与登录', signal => api('/agents', { signal }), setAgents, options.signal);
  const loadDoctor = diagnose(doctor, '环境自检', signal => api('/doctor', { signal }), setChecks, options.signal);
  const refresh = () => { loadAgents(); loadDoctor(); };

  async function loadPreferences() {
    form.replaceChildren(element('p', 'muted', '正在加载设置…'));
    try {
      const config = await api('/settings', options);
      if (!options.signal.aborted) preferences(form, config, refresh);
    } catch (error) {
      if (options.signal.aborted) return;
      const retry = element('button', '', '重新加载');
      retry.addEventListener('click', loadPreferences);
      form.replaceChildren(element('p', 'error', error.message), retry);
    }
  }

  loadPreferences();
  refresh();
  return () => controller.abort();
}
