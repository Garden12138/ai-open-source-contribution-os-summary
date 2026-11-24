const entities = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const characters = Object.fromEntries(Object.entries(entities).map(([text, html]) => [html, text]));
export const escapeHTML = text => String(text).replace(/[&<>"']/g, character => entities[character]);

export function externalURL(value) {
  if (typeof value !== 'string' || !/^https?:\/\//i.test(value) || /[\u0000-\u0020\u007f]/.test(value)) return;
  try {
    const url = new URL(value);
    if (['https:', 'http:'].includes(url.protocol) && !url.username && !url.password) return url.href;
  } catch {
    // Invalid links are displayed as text.
  }
}

function inline(escaped) {
  return escaped.replace(/`([^`\n]+)`|\*\*([^*\n]+)\*\*|\[([^\]\n]+)\]\(([^\s)]+)\)/g,
    (token, code, strong, label, href) => {
      if (code) return `<code>${code}</code>`;
      if (strong) return `<strong>${strong}</strong>`;
      const url = externalURL(href.replace(/&(?:amp|lt|gt|quot|#39);/g, entity => characters[entity]));
      return url ? `<a href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer">${label}</a>` : token;
    });
}

export function renderMarkdown(text) {
  const output = [];
  let block = '';
  const lines = [];
  const flush = () => {
    if (block === 'code') output.push(`<pre><code>${lines.join('\n')}</code></pre>`);
    if (block === 'paragraph') output.push(`<p>${lines.map(inline).join('<br>')}</p>`);
    if (block === 'ul' || block === 'ol') {
      output.push(`<${block}>${lines.map(line => `<li>${inline(line)}</li>`).join('')}</${block}>`);
    }
    lines.length = 0;
    block = '';
  };
  for (const line of escapeHTML(text).replace(/\r\n?/g, '\n').split('\n')) {
    if (/^\s*```/.test(line)) {
      const closing = block === 'code';
      flush();
      if (!closing) block = 'code';
    } else if (block === 'code') {
      lines.push(line);
    } else if (!line.trim()) {
      flush();
    } else {
      const item = line.match(/^\s*(?:([-*+])|\d+\.)\s+(.+)$/);
      const next = item ? (item[1] ? 'ul' : 'ol') : 'paragraph';
      if (next !== block) flush();
      block = next;
      lines.push(item ? item[2] : line);
    }
  }
  flush();
  return output.join('');
}
