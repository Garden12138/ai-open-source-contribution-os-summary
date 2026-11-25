import { api, element } from './api.js';

const shortcuts = [
  '按这个方案开始实现', '跑一下相关测试', '自查一遍改动', '缩小改动范围', '同步上游最新代码',
];

export function isSendKey({ key, shiftKey, isComposing, keyCode }) {
  return key === 'Enter' && !shiftKey && !isComposing && keyCode !== 229;
}

export function mountComposer(root, path, refresh) {
  const form = element('form', 'composer');
  const phrases = element('div', 'phrases');
  const input = element('textarea');
  input.rows = 3;
  input.name = 'message';
  input.maxLength = 131072;
  input.placeholder = '给 Agent 发消息…';
  input.setAttribute('aria-label', '给 Agent 发消息');
  const action = (title, className) => {
    const button = element('button', className, title);
    button.type = 'button';
    return button;
  };
  const send = action('发送', 'primary');
  send.type = 'submit';
  const interrupt = action('打断并发送');
  const stop = action('停止');
  const resume = action('继续');
  const controls = element('div', 'composer-actions');
  const agents = element('select');
  agents.setAttribute('aria-label', '接手任务的 Agent');
  const handoff = action('换 Agent 继续');
  controls.append(agents, handoff, stop, resume, interrupt, send);
  const status = element('p', 'muted');
  status.setAttribute('role', 'status');
  form.append(phrases, input, controls, status);
  root.append(form);

  let task;
  let busy = false;
  let feedback = '';
  let failed = false;
  let pendingMessage;
  let pendingSwitch;

  function update() {
    const ready = ['ready', 'published'].includes(task?.status) && !task.deleting;
    const running = task?.agent_state === 'running';
    const queued = task?.turns.filter(turn => turn.status === 'queued').length || 0;
    const stopping = running || task?.agent_state === 'waiting_retry';
    const canSend = ready && !busy && Boolean(input.value.trim());
    input.disabled = !ready;
    phrases.querySelectorAll('button').forEach(button => { button.disabled = !ready; });
    feedbackShortcut.hidden = task?.status !== 'published';
    send.disabled = !canSend;
    interrupt.hidden = !running;
    interrupt.disabled = !canSend;
    stop.hidden = !stopping;
    stop.disabled = busy || !ready;
    const pausedSetup = task?.status === 'setup_failed' && !task.deleting
      && ['sandbox_disk_limit', 'sandbox_disk_unavailable'].includes(task.last_error_code);
    resume.hidden = (!ready && !pausedSetup) || stopping || task?.agent_state === 'queued';
    resume.disabled = busy;
    agents.hidden = handoff.hidden = !ready || !task?.switch_agents?.length;
    agents.disabled = handoff.disabled = !ready || busy || stopping || queued > 0;
    handoff.disabled ||= !agents.value;
    const activity = ready
      ? [running ? '运行中，发送的新消息将排队。' : '', queued ? `${queued} 轮等待执行。` : ''].filter(Boolean).join(' ')
      : (task?.status === 'setup_failed' ? '工作区准备失败。' : '工作区就绪后即可发送消息。');
    status.classList.toggle('error', failed);
    status.textContent = feedback || activity;
  }

  async function mutate(endpoint, body) {
    if (busy) return;
    busy = true;
    feedback = '';
    failed = false;
    update();
    try {
      const result = await api(`${path}/${endpoint}`, { method: 'POST', body });
      if (endpoint === 'messages') {
        if (input.value === body.text) input.value = '';
        pendingMessage = undefined;
      }
      if (endpoint === 'switch-agent') pendingSwitch = undefined;
      feedback = endpoint === 'interrupt' && !result.interrupted ? '当前没有可停止的轮次。' : '';
      await refresh();
    } catch (error) {
      failed = true;
      feedback = error.message;
    } finally {
      busy = false;
      update();
    }
  }

  function sendMessage(delivery) {
    if (send.disabled) return;
    const text = input.value;
    if (pendingMessage?.text !== text) pendingMessage = { text, client_message_id: crypto.randomUUID() };
    mutate('messages', { ...pendingMessage, delivery });
  }

  let feedbackShortcut;
  for (const phrase of [...shortcuts, '处理 review 意见']) {
    const button = action(phrase);
    if (phrase === '处理 review 意见') feedbackShortcut = button;
    button.addEventListener('click', () => {
      input.value = phrase;
      input.focus();
      update();
    });
    phrases.append(button);
  }
  input.addEventListener('input', () => {
    feedback = '';
    failed = false;
    update();
  });
  input.addEventListener('keydown', event => {
    if (!isSendKey(event)) return;
    event.preventDefault();
    sendMessage('queue');
  });
  form.addEventListener('submit', event => {
    event.preventDefault();
    sendMessage('queue');
  });
  interrupt.addEventListener('click', () => sendMessage('interrupt'));
  stop.addEventListener('click', () => mutate('interrupt'));
  resume.addEventListener('click', () => mutate('continue'));
  agents.addEventListener('change', update);
  handoff.addEventListener('click', () => {
    if (pendingSwitch?.agent !== agents.value) {
      pendingSwitch = { agent: agents.value, client_message_id: crypto.randomUUID() };
    }
    mutate('switch-agent', pendingSwitch);
  });
  update();

  return {
    update(next) {
      if (JSON.stringify(next.switch_agents) !== JSON.stringify(task?.switch_agents)) {
        agents.replaceChildren(new Option('选择 Agent', ''),
          ...(next.switch_agents || []).map(agent => new Option(agent.name, agent.id)));
      }
      task = next;
      update();
    },
    resume() { return mutate('continue'); },
    remove() { form.remove(); },
  };
}
