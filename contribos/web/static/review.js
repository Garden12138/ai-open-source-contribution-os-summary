import { api, element } from './api.js';

const labels = {
  running: '审查中', invalid: '审查无效', failed: '无法使用审查结果',
  approve: '审查通过', request_changes: '建议修改',
};

export function mountReview(root, path) {
  const controller = new AbortController();
  let task, reviews, failure, timer;
  let pending = false;
  let version = 0;

  async function refresh() {
    const revision = ++version;
    try {
      const result = await api(`${path}/reviews`, { signal: controller.signal });
      if (revision !== version || controller.signal.aborted) return;
      reviews = result.reviews;
    } catch (error) {
      if (revision !== version || controller.signal.aborted) return;
      failure = error.message;
    }
    paint();
  }

  async function submit(suffix) {
    if (pending) return;
    pending = true;
    failure = undefined;
    paint();
    try {
      await api(path + suffix, { method: 'POST', signal: controller.signal });
    } catch (error) {
      failure = error.message;
    } finally {
      pending = false;
      if (!controller.signal.aborted) await refresh();
    }
  }

  function paint() {
    root.replaceChildren();
    if (failure) root.append(element('p', 'error', failure));
    if (!reviews) return;
    const busy = pending || reviews.some(review => review.status === 'running') ||
      ['queued', 'running', 'waiting_retry'].includes(task?.agent_state);
    const start = element('button', 'primary', '开始独立审查');
    start.type = 'button';
    start.disabled = busy || !['ready', 'published'].includes(task?.status);
    start.addEventListener('click', () => submit('/review'));
    root.append(start);
    for (const review of reviews) {
      const card = element('article', 'activity review-card');
      const status = review.status === 'completed' ? review.verdict : review.status;
      card.append(element('h3', '', `${labels[status] || status} · ${review.agent_name}`));
      if (review.summary) card.append(element('p', 'verbatim', review.summary));
      if (review.findings.length) {
        const findings = element('ul');
        for (const finding of review.findings) {
          const where = finding.line == null ? finding.file : `${finding.file}:${finding.line}`;
          const entry = element('li');
          entry.append(element('strong', '', `${finding.severity} · ${where}`), element('p', '', finding.message));
          findings.append(entry);
        }
        card.append(findings);
        if (review.status === 'completed') {
          const apply = element('button', '', '按审查意见修改');
          apply.type = 'button';
          apply.disabled = busy;
          apply.addEventListener('click', () => submit(`/reviews/${encodeURIComponent(review.id)}/apply`));
          card.append(apply);
        }
      }
      root.append(card);
    }
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
