export async function api(path, { method = 'GET', body, signal } = {}) {
  const headers = {};
  if (!['GET', 'HEAD'].includes(method)) {
    const cookie = document.cookie.split(/;\s*/).find(value => value.startsWith('contribos_csrf='));
    headers['X-CSRF-Token'] = cookie?.slice('contribos_csrf='.length) || '';
  }
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(`/api${path}`, { method, headers, body: JSON.stringify(body), signal });
  if (response.ok) return response.json();
  const failure = await response.json().catch(() => ({}));
  const message = response.status === 401
    ? '会话已失效，请从 contribos serve 打印的一次性登录链接重新打开页面。'
    : failure.error?.message || '请求失败，请重试。';
  throw Object.assign(new Error(message), { code: failure.error?.code, status: response.status });
}

export const el = id => document.getElementById(id);
export const taskPath = id => `/tasks/${encodeURIComponent(id)}`;
export function isolationAvailable(agent, kind) {
  return Boolean(agent?.available_isolation.includes(kind)) &&
    (kind !== 'host' || (agent.installed && ['ok', 'unknown'].includes(agent.status)));
}

export const isolationLabel = kind => ({ host: '本机直跑', sbx: '沙箱', container: '容器沙箱' }[kind] || kind);
export const agentLogin = status => ({ logged_in: '已登录', not_logged_in: '未登录', unknown: '登录状态未知' }[status] || '登录状态未知');

export function element(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}
