function gitPath(raw) {
  const path = raw.split('\t', 1)[0];
  if (!path.startsWith('"') || !path.endsWith('"')) return path;
  const encoder = new TextEncoder();
  const bytes = [];
  const escapes = { a: '\x07', b: '\b', t: '\t', n: '\n', v: '\v', f: '\f', r: '\r' };
  for (const token of path.slice(1, -1).matchAll(/\\([0-7]{3}|.)|([^\\]+)/g)) {
    if (token[1]?.length === 3) bytes.push(parseInt(token[1], 8));
    else bytes.push(...encoder.encode(token[2] ?? escapes[token[1]] ?? token[1]));
  }
  return new TextDecoder().decode(Uint8Array.from(bytes));
}

export function parseDiff(text, stats = []) {
  const groups = [];
  let group;
  let inHunk = false;
  for (const line of text.split('\n')) {
    if (line.startsWith('diff --git ') || (!group && line.length)) {
      const quotedTarget = line.match(/ ("b\/(?:[^"\\]|\\.)*")$/)?.[1];
      const path = quotedTarget ? gitPath(quotedTarget).slice(2) : gitPath(stats[groups.length]?.path || '');
      group = { path, lines: [], additions: 0, deletions: 0 };
      groups.push(group);
      inHunk = false;
    }
    if (!group) continue;
    let kind = 'context';
    if (line.startsWith('@@')) {
      inHunk = true;
      kind = 'hunk';
    } else if (inHunk && line.startsWith('+')) {
      group.additions++;
      kind = 'added';
    } else if (inHunk && line.startsWith('-')) {
      group.deletions++;
      kind = 'removed';
    } else if (!inHunk && /^(?:\+\+\+ |--- |rename to |copy to )/.test(line)) {
      const path = gitPath(line.replace(/^(?:\+\+\+ |--- |rename to |copy to )/, ''));
      if (path !== '/dev/null') group.path = line.startsWith('+++ ') || line.startsWith('--- ')
        ? path.replace(/^[ab]\//, '') : path;
    }
    group.lines.push({ text: line, kind });
  }
  return groups;
}
