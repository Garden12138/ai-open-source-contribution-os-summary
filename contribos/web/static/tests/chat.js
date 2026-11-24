const test = require('node:test');
const assert = require('node:assert/strict');

test('Markdown escapes HTML in every block and supports the display subset', async () => {
  const { renderMarkdown } = await import('../markdown.js');
  assert.equal(renderMarkdown('**bold** and `<tag>`\n\n- one\n- two\n\n1. first'),
    '<p><strong>bold</strong> and <code>&lt;tag&gt;</code></p><ul><li>one</li><li>two</li></ul><ol><li>first</li></ol>');
  assert.equal(renderMarkdown('```html\n<script>alert(1)</script>\n```'),
    '<pre><code>&lt;script&gt;alert(1)&lt;/script&gt;</code></pre>');
  assert.equal(renderMarkdown('<img src=x onerror=alert(1)>'), '<p>&lt;img src=x onerror=alert(1)&gt;</p>');
  assert.equal(renderMarkdown('```js\nconst a = "<";'), '<pre><code>const a = &quot;&lt;&quot;;</code></pre>');
});

test('Markdown link URLs cannot escape quoted attributes or use other schemes', async () => {
  const { renderMarkdown } = await import('../markdown.js');
  for (const href of ['JaVaScRiPt:alert(1)', 'java&#x73;cript:alert(1)', 'data:text/html,<script>', '//x.test',
    'javascript\t:alert(1)', 'https://example.test\u0000/', 'file:///example']) {
    assert.doesNotMatch(renderMarkdown(`[link](${href})`), /<a\b/);
  }
  assert.equal(renderMarkdown('[<img>](https://example.test/"x?one=1&two=2)'),
    '<p><a href="https://example.test/%22x?one=1&amp;two=2" target="_blank" rel="noopener noreferrer">&lt;img&gt;</a></p>');
});

test('SSE replay deduplicates durable events and replaces streaming fragments with the snapshot and final text', async () => {
  const { timeline } = await import('../chat-events.js');
  const stream = timeline();
  const event = (type, payload, seq = null) => ({ turn_id: 't', type, payload, seq });
  assert.equal(stream.receive(event('message.user', { text: 'hi' }, 6)), true);
  assert.equal(stream.receive(event('message.user', { text: 'hi' }, 6)), false);
  stream.receive(event('message.delta', { text: 'part' }));
  stream.receive(event('stream.snapshot', { items: [event('message.delta', { text: 'partial' })] }));
  stream.receive(event('message.delta', { text: ' done' }));
  assert.equal([...stream.live.values()][0].payload.text, 'partial done');
  stream.receive(event('message.agent', { text: 'partial done' }, 7));
  assert.equal(stream.live.size, 0);
  assert.deepEqual([...stream.history.values()].map(item => item.payload.text), ['hi', 'partial done']);
});

test('SSE history preserves command arguments through completion and discards interrupted transients', async () => {
  const { timeline } = await import('../chat-events.js');
  const stream = timeline();
  stream.receive({ type: 'command.started', seq: 1, turn_id: 't', payload: { tool_call_id: 'one', command: 'pytest' } });
  stream.receive({ type: 'reasoning.delta', turn_id: 't', payload: { text: 'Running tests' } });
  stream.receive({ type: 'command.completed', seq: 2, turn_id: 't', payload: { tool_call_id: 'one', exit_code: 0 } });
  stream.receive({ type: 'turn.interrupted', seq: 3, turn_id: 't', payload: {} });
  assert.deepEqual([...stream.history.values()][0].payload, { tool_call_id: 'one', command: 'pytest', exit_code: 0 });
  assert.equal(stream.live.size, 0);
});

test('request resolutions and expirations update the original card on replay', async () => {
  const { timeline } = await import('../chat-events.js');
  const stream = timeline();
  stream.receive({ type: 'permission.requested', seq: 1, payload: { request_id: 'a', options: ['allow'] } });
  stream.receive({ type: 'question.asked', seq: 2, payload: { request_id: 'b', message: 'Scope?' } });
  stream.receive({ type: 'system.request_expired', seq: 3, payload: { request_id: 'a' } });
  stream.receive({ type: 'question.answered', seq: 4, payload: { request_id: 'b' } });
  assert.equal(stream.history.size, 2);
  assert.deepEqual(stream.history.get('request:a').payload.options, ['allow']);
  assert.equal(stream.history.get('request:a').payload.resolution, 'system.request_expired');
  assert.equal(stream.history.get('request:b').payload.resolution, 'question.answered');
});

test('composer sends Enter, leaves Shift+Enter and IME composition to the textarea', async () => {
  const { isSendKey } = await import('../composer.js');
  assert.equal(isSendKey({ key: 'Enter' }), true);
  assert.equal(isSendKey({ key: 'Enter', shiftKey: true }), false);
  assert.equal(isSendKey({ key: 'Enter', isComposing: true }), false);
  assert.equal(isSendKey({ key: 'Enter', keyCode: 229 }), false);
});
