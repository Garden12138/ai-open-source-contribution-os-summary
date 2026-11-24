const test = require('node:test');
const assert = require('node:assert/strict');

test('unified diff preserves file groups, Git-quoted paths, and counts only hunk lines', async () => {
  const { parseDiff } = await import('../diff.js');
  const files = parseDiff([
    'diff --git a/old b/new',
    'rename from old',
    'rename to new file',
    '--- a/old',
    '+++ b/new file\t',
    '@@ -1 +1 @@',
    '-old',
    '+new',
    'diff --git "a/\\303\\251.txt" "b/\\303\\251.txt"',
    'new file mode 100644',
    '--- /dev/null',
    '+++ "b/\\303\\251.txt"',
    '@@ -0,0 +1 @@',
    '+line',
  ].join('\n'));
  assert.deepEqual(files.map(file => [file.path, file.additions, file.deletions]),
    [['new file', 1, 1], ['é.txt', 1, 0]]);
  assert.equal(files[0].lines[5].kind, 'hunk');
});

test('binary and metadata-only changes remain visible and do not create hunk counts', async () => {
  const { parseDiff } = await import('../diff.js');
  const [file] = parseDiff('diff --git a/icon.png b/icon.png\nGIT binary patch\nliteral 8', [{ path: 'icon.png' }]);
  assert.equal(file.path, 'icon.png');
  assert.equal(file.lines[1].text, 'GIT binary patch');
  assert.equal(file.additions, 0);
  const [metadata] = parseDiff('diff --git "a/\\303\\251" "b/\\303\\251"\nold mode 100644\nnew mode 100755');
  assert.equal(metadata.path, 'é');
  assert.deepEqual(parseDiff(''), []);
});
