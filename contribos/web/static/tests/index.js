const test = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');

const html = readFileSync(join(__dirname, '..', 'index.html'), 'utf8');

test('placeholder provides a named main landmark and document language', () => {
  assert.match(html, /<html\s+lang="zh-CN"/);
  assert.match(html, /<title>[^<]+<\/title>/);
  assert.match(html, /<main>[\s\S]*<h1>[^<]+<\/h1>[\s\S]*<\/main>/);
});

test('page assets follow the same-origin CSP without inline execution', () => {
  assert.doesNotMatch(html, /<style\b|\sstyle\s*=|\son[a-z]+\s*=/i);
  for (const [tag] of html.matchAll(/<(?:script|link)\b[^>]*>/gi)) {
    const source = tag.match(/(?:src|href)="([^"]+)"/);
    assert.ok(source, 'assets must be external files');
    assert.match(source[1], /^\/static\//);
    assert.doesNotMatch(source[1], /[<>"']/);
  }
  assert.doesNotMatch(html, /(?:src|href)\s*=\s*["']\s*javascript:/i);
});
