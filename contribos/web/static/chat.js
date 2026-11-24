import { element } from './api.js';
import { timeline } from './chat-events.js';
import { renderMarkdown } from './markdown.js';
import { requestCard } from './requests.js';

const eventTypes = [
  'message.user', 'message.agent', 'message.delta', 'reasoning.delta', 'reasoning.summary',
  'command.started', 'command.completed', 'files.changed', 'tool.activity', 'plan.updated',
  'task.preparing', 'task.ready', 'task.setup_failed', 'task.delete_failed',
  'permission.requested', 'permission.resolved', 'question.asked', 'question.answered',
  'system.request_expired', 'system.retry_scheduled', 'system.recovered', 'system.session_recreated',
  'system.warning', 'system.runtime_failed', 'system.recovery_failed', 'system.cancel_forced', 'system.agent_exited',
  'turn.started', 'turn.completed', 'turn.failed', 'turn.interrupted',
  'diff.updated', 'issue.updated', 'issue.refresh_failed',
  'mode.changed', 'usage.updated', 'agent.other', 'stream.snapshot',
];

const states = { pending: '等待', in_progress: '进行中', completed: '完成', failed: '失败' };
const label = value => Object.hasOwn(states, value) ? states[value] : value;

function disclosure(title, content) {
  const card = element('details', 'activity');
  card.append(element('summary', '', title), content);
  return card;
}

function list(items, className) {
  const node = element('ul', className);
  for (const item of items) node.append(element('li', '', item));
  return node;
}

function systemMessage(type, payload) {
  switch (type) {
    case 'task.preparing': return '正在获取 Issue 并准备工作区。';
    case 'task.ready': return '工作区已就绪。';
    case 'turn.interrupted': return '本轮已停止，工作区改动已保留。';
    case 'system.retry_scheduled': return `${payload.delay_seconds} 秒后自动重试（${payload.retry_count}/3）。`;
    case 'system.recovered': return 'Agent 会话已恢复。';
    case 'system.session_recreated': return 'Agent 已建立新会话，并带入任务回顾。';
    case 'system.cancel_forced': return '已停止 Agent 进程，下一轮将恢复会话。';
    case 'system.agent_exited': return 'Agent 进程已退出，正在恢复会话。';
    case 'system.warning': return payload.message || `提示 · ${payload.code || 'Agent 活动出现异常'}`;
    default: return payload.message || payload.error_code || '暂时无法继续，请重试。';
  }
}

function cardFor(event, path, resume) {
  const { type, payload } = event;
  const { text = '', title, status } = payload;
  if (type === 'permission.requested' || type === 'question.asked') return requestCard(event, path);
  if (type === 'permission.resolved' && payload.auto) return element('p', 'activity', '权限已自动处理。');
  if (type.startsWith('message.')) {
    const card = element('article', type === 'message.user' ? 'message user-message' : 'message');
    card.append(element('h3', '', type === 'message.user' ? '你' : 'Agent'));
    const content = element('div', 'markdown');
    content.innerHTML = renderMarkdown(text);
    card.append(content);
    return card;
  }
  if (type.startsWith('reasoning.')) return disclosure('思考摘要', element('p', 'verbatim', text));
  if (type.startsWith('command.')) {
    const titleText = [title || '命令', label(status), payload.exit_code == null ? '' : `退出码 ${payload.exit_code}`]
      .filter(Boolean).join(' · ');
    const card = disclosure(titleText, element('pre', '', payload.output_tail || '暂无输出。'));
    const command = payload.command || title;
    if (command) card.children[0].after(element('code', 'command-line', Array.isArray(command) ? command.join(' ') : command));
    return card;
  }
  if (type === 'files.changed') {
    const card = element('div', 'activity');
    card.append(element('strong', '', `文件改动${status ? ` · ${label(status)}` : ''}`));
    if (payload.paths?.length) card.append(list(payload.paths, 'paths'));
    return card;
  }
  if (type === 'tool.activity' || type === 'agent.other') {
    return disclosure(title || 'Agent 活动', element('p', '', [payload.kind || payload.type, label(status)].filter(Boolean).join(' · ')));
  }
  if (type === 'plan.updated') {
    const card = element('div', 'activity');
    card.append(element('strong', '', '待办'), list((payload.entries || []).map(entry =>
      `${label(entry.status) || '等待'} · ${entry.content || ''}`)));
    return card;
  }
  if (type === 'task.preparing' || type === 'task.ready' || type === 'task.delete_failed' || type === 'task.setup_failed' ||
      type === 'turn.failed' || type === 'turn.interrupted' || type.startsWith('system.')) {
    const failed = type.endsWith('failed');
    const card = element('div', failed ? 'activity error' : 'activity');
    card.append(element('p', '', systemMessage(type, payload)));
    if (failed && !['task.setup_failed', 'task.delete_failed'].includes(type)) {
      const button = element('button', '', '继续');
      button.type = 'button';
      button.addEventListener('click', () => resume(button));
      card.append(button);
    }
    return card;
  }
}

export function mountChat(root, path, { onEvent, onResume } = {}) {
  const connection = element('p', 'connection muted', '正在连接对话…');
  connection.setAttribute('role', 'status');
  const feed = element('div', 'feed');
  feed.setAttribute('aria-label', '对话记录');
  const durable = element('div', 'timeline');
  const transient = element('div', 'timeline');
  feed.append(durable, transient);
  root.replaceChildren(connection, feed);
  const state = timeline();
  const rendered = new Map();
  let frame;
  let closed = false;

  async function resume(button) {
    button.disabled = true;
    try {
      await onResume?.();
    } finally {
      button.disabled = false;
    }
  }

  function sync(parent, entries, prefix) {
    for (const [key, record] of rendered) {
      if (key.startsWith(prefix) && !entries.has(key.slice(prefix.length))) {
        record.node?.remove();
        rendered.delete(key);
      }
    }
    for (const [key, event] of entries) {
      const qualified = prefix + key;
      const old = rendered.get(qualified);
      if (old?.event === event) continue;
      const node = cardFor(event, path, resume);
      rendered.set(qualified, { event, node });
      if (!node) continue;
      if (old?.node) {
        if (node.tagName === 'DETAILS') node.open = old.node.open;
        old.node.replaceWith(node);
      } else {
        parent.append(node);
      }
    }
  }

  function paint() {
    frame = undefined;
    const follow = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 90;
    sync(durable, state.history, 'history:');
    sync(transient, state.live, 'live:');
    if (follow) feed.scrollTop = feed.scrollHeight;
    if (!durable.children.length && !transient.children.length && connection.hidden) {
      transient.textContent = '暂无对话。';
    } else if (transient.firstChild?.nodeType === Node.TEXT_NODE) {
      transient.firstChild.remove();
    }
  }

  function schedulePaint() {
    frame ??= requestAnimationFrame(paint);
  }

  const source = new EventSource(`/api${path}/stream`);
  source.onopen = () => {
    connection.hidden = true;
    schedulePaint();
  };
  source.onerror = () => {
    state.clearLive();
    connection.textContent = '对话连接已断开，正在重连…';
    connection.hidden = false;
    if (source.readyState === EventSource.CLOSED) {
      connection.textContent = '无法连接对话，请重新加载页面。';
    }
    schedulePaint();
  };
  for (const type of eventTypes) {
    source.addEventListener(type, message => {
      if (closed) return;
      const event = JSON.parse(message.data);
      if (state.receive(event, event.seq == null ? undefined : message.lastEventId)) {
        schedulePaint();
        onEvent?.(event);
      }
    });
  }
  return () => {
    closed = true;
    source.close();
    if (frame !== undefined) cancelAnimationFrame(frame);
    root.replaceChildren();
  };
}
