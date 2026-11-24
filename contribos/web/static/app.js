import { api, el, element, isolationLabel, taskPath } from './api.js';
import { mountDiscovery } from './discovery.js';
import { bindNewTask, openNewTask } from './new-task.js';
import { mountChat } from './chat.js';
import { mountComposer } from './composer.js';
import { mountDetails } from './details.js';
import { mountSettings } from './settings.js';

let activeId;
let taskList = [];
let detailVersion = 0;
let closeChat;
let composer;
let details;
let closeSettings;
let closeDiscovery;
let refreshTimer;
const statusLabels = {
  preparing: '正在准备', setup_failed: '准备失败', published: '已发布', idle: '空闲',
  queued: '已排队', running: '运行中', waiting_retry: '等待重试', error: '需要处理',
};
const taskTitle = el('task-title');
const taskStatus = el('task-status');
const meta = el('task-meta');

function stateLabel(task) {
  return statusLabels[task.status === 'ready' ? task.agent_state : task.status] || task.status;
}

function renderList() {
  const list = el('tasks');
  const entries = taskList.map(task => {
    const link = element('a', 'task-link');
    link.href = `#task/${encodeURIComponent(task.id)}`;
    if (task.id === activeId) link.setAttribute('aria-current', 'page');
    link.append(element('strong', '', task.title),
      element('span', '', `${task.repo_full_name} #${task.issue_number} · ${stateLabel(task)}`));
    const item = element('li');
    item.append(link);
    return item;
  });
  list.replaceChildren(...entries);
}

async function loadList() {
  try {
    const { tasks, demo } = await api('/tasks');
    taskList = tasks;
    el('demo-mode').hidden = !demo;
    el('tasks-status').textContent = taskList.length ? '' : '暂无任务。';
    el('retry-tasks').hidden = true;
    renderList();
  } catch (error) {
    el('tasks-status').textContent = error.message;
    el('retry-tasks').hidden = false;
  }
}

async function loadTask(id) {
  const version = ++detailVersion;
  try {
    const task = await api(taskPath(id));
    if (version !== detailVersion) return;
    composer?.update(task);
    details?.update(task);
    taskTitle.textContent = task.title;
    const isolation = element('span', task.isolation === 'host' ? 'badge warning' : 'badge', isolationLabel(task.isolation));
    meta.replaceChildren(element('span', 'badge', task.agent_name || task.agent_profile), isolation);
    taskStatus.textContent = task.last_error_message
      ? `${stateLabel(task)} · ${task.last_error_message}`
      : `${task.repo_full_name} #${task.issue_number} · ${stateLabel(task)}`;
  } catch (error) {
    if (version === detailVersion) taskStatus.textContent = error.message;
  }
}

function navigate() {
  closeChat?.();
  closeChat = undefined;
  composer?.remove();
  composer = undefined;
  details?.close();
  details = undefined;
  closeSettings?.();
  closeSettings = undefined;
  clearTimeout(refreshTimer);
  const hash = location.hash;
  const settings = hash === '#settings';
  const discovery = !hash || hash === '#' || hash === '#discovery';
  closeDiscovery?.();
  closeDiscovery = undefined;
  el('discovery').hidden = !discovery;
  el('settings').hidden = !settings;
  el('workspace').hidden = settings || discovery;
  const nav = discovery ? 'discovery-nav' : settings ? 'settings-nav' : 'tasks-nav';
  for (const id of ['discovery-nav', 'tasks-nav', 'settings-nav']) {
    if (id === nav) el(id).setAttribute('aria-current', 'page');
    else el(id).removeAttribute('aria-current');
  }
  if (discovery) closeDiscovery = mountDiscovery(openNewTask);
  if (settings) closeSettings = mountSettings(el('settings-body'));
  activeId = hash.match(/^#task\/([a-zA-Z0-9_-]+)$/)?.[1];
  ++detailVersion;
  renderList();
  meta.replaceChildren();
  taskTitle.textContent = activeId ? '正在加载任务…' : '贡献任务';
  taskStatus.textContent = activeId ? '' : '选择任务或粘贴 GitHub Issue 链接开始。';
  if (activeId) {
    const id = activeId;
    loadTask(id);
    closeChat = mountChat(el('chat'), taskPath(id), {
      onEvent(event) {
        details?.event(event);
        if (/^(?:task|turn|system)\./.test(event.type) || ['message.user', 'usage.updated'].includes(event.type)) {
          clearTimeout(refreshTimer);
          refreshTimer = setTimeout(() => loadTask(id), 150);
        }
      },
      onResume: () => composer?.resume(),
    });
    composer = mountComposer(el('chat'), taskPath(id), () => {
      if (activeId === id) return loadTask(id);
    });
    details = mountDetails(el('details'), taskPath(id), () => loadTask(id));
  }
}

bindNewTask(id => {
  location.hash = `task/${encodeURIComponent(id)}`;
  loadList();
});
el('retry-tasks').addEventListener('click', loadList);
window.addEventListener('hashchange', navigate);
navigate();
loadList();
setInterval(() => {
  if (document.hidden || el('workspace').hidden) return;
  loadList();
  if (activeId) loadTask(activeId);
}, 5000);
