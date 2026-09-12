import { requestArtifact } from "./api.js?v=analysis-context-v2";

const labels = {added: "新增", modified: "修改", deleted: "删除"};
const PAGE_SIZE = 300;

// Parse the Runner's exact diff dialect against its authoritative file inventory.
// A malformed patch must never acquire invented line numbers or counts.
export function parseExecutionChanges(text, inventory) {
  if (!inventory || !Array.isArray(inventory.changed_paths)
      || !Array.isArray(inventory.baseline_inventory) || !Array.isArray(inventory.result_inventory)) {
    throw new Error("文件清单格式无效");
  }
  const before = new Map(inventory.baseline_inventory.map(file => [file.path, file]));
  const after = new Map(inventory.result_inventory.map(file => [file.path, file]));
  const files = inventory.changed_paths.map(path => {
    if (typeof path !== "string" || !path || (!before.has(path) && !after.has(path))) {
      throw new Error("文件清单路径无效");
    }
    return {path, status: !before.has(path) ? "added" : !after.has(path) ? "deleted" : "modified",
      modeChanged: before.has(path) && after.has(path) && before.get(path).executable !== after.get(path).executable,
      additions: 0, deletions: 0, rows: [], unavailable: false};
  });
  if (new Set(files.map(file => file.path)).size !== files.length) throw new Error("文件清单路径重复");
  try {
    let index = 0;
    const lines = text.split("\n");
    if (lines.at(-1) === "") lines.pop();
    for (const file of files) {
      const path = file.path;
      if (/[\r\n]/.test(path) || lines[index++] !== `diff --git a/${path} b/${path}`) throw new Error();
      const old = before.get(path), next = after.get(path);
      const mode = value => value.executable ? "100755" : "100644";
      const metadata = !old ? [`new file mode ${mode(next)}`] : !next ? [`deleted file mode ${mode(old)}`]
        : file.modeChanged ? [`old mode ${mode(old)}`, `new mode ${mode(next)}`] : [];
      // Older deterministic artifacts may omit mode headers.
      for (const expected of metadata) if (lines[index] === expected) index++;
      const oldPath = old ? `a/${path}` : "/dev/null";
      const newPath = next ? `b/${path}` : "/dev/null";
      if (lines[index] === `Binary files ${oldPath} and ${newPath} differ`) {
        index++; file.unavailable = true; file.additions = null; file.deletions = null;
        continue;
      }
      if (lines[index] === `--- ${oldPath}`) {
        index++;
        if (lines[index++] !== `+++ ${newPath}`) throw new Error();
      } else if (old?.sha256 !== next?.sha256 && (old?.size || next?.size)) throw new Error();
      while (lines[index]?.startsWith("@@ ")) {
        const match = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)$/.exec(lines[index]);
        if (!match) throw new Error();
        let oldLine = Number(match[1]), newLine = Number(match[3]);
        let oldLeft = Number(match[2] ?? 1), newLeft = Number(match[4] ?? 1);
        if (![oldLine, newLine, oldLeft, newLeft].every(Number.isSafeInteger)) throw new Error();
        if (oldLeft && !oldLine || newLeft && !newLine) throw new Error();
        file.rows.push({kind: "hunk", text: lines[index++]});
        while (oldLeft || newLeft) {
          const line = lines[index++];
          if (line === undefined) throw new Error();
          const kind = {" ": "context", "+": "add", "-": "delete"}[line[0]];
          if (!kind) throw new Error();
          const row = {kind, text: line.slice(1), oldLine: null, newLine: null};
          if (kind !== "add") { if (--oldLeft < 0) throw new Error(); row.oldLine = oldLine++; }
          if (kind !== "delete") { if (--newLeft < 0) throw new Error(); row.newLine = newLine++; }
          if (kind === "add") file.additions++;
          if (kind === "delete") file.deletions++;
          file.rows.push(row);
          if (lines[index] === "\\ No newline at end of file") {
            file.rows.push({kind: "note", text: "文件末尾无换行"}); index++;
          }
        }
      }
      if (!file.rows.length && old?.sha256 !== next?.sha256 && (old?.size || next?.size)) throw new Error();
    }
    if (index !== lines.length) throw new Error();
    return {files, raw: text, malformed: false};
  } catch {
    return {files: files.map(file => ({...file, additions: null, deletions: null, rows: [], unavailable: true})),
      raw: text, malformed: true};
  }
}

function node(tag, text = "", className = "") {
  const result = document.createElement(tag);
  result.textContent = text;
  result.className = className;
  return result;
}
function button(text, action) {
  const result = node("button", text, "changes-button");
  result.type = "button";
  result.addEventListener("click", action);
  return result;
}
function stats(file) {
  return file.additions === null ? "文本行数不可用" : `+${file.additions} / −${file.deletions}`;
}
function title(model) {
  if (!model.files.length) return "本次执行没有代码修改";
  const known = model.files.every(file => file.additions !== null);
  const counts = known ? ` · +${model.files.reduce((n, f) => n + f.additions, 0)} / −${model.files.reduce((n, f) => n + f.deletions, 0)}` : " · 部分文本行数不可用";
  return `已修改 ${model.files.length} 个文件${counts}`;
}

export function implementationArtifacts(detail) {
  const manifests = (detail?.artifact_manifests || []).filter(item => item.stage === "implement");
  const manifest = manifests.at(-1);
  if (!manifest) return null;
  if (manifest.execution_attempt_id !== detail.id) throw new Error("执行结果归属无效");
  const diff = manifest.entries.find(entry => entry.role === "unified-diff");
  const inventory = manifest.entries.find(entry => entry.role === "file-inventory");
  if (!diff || !inventory) throw new Error("执行结果不完整");
  for (const entry of [diff, inventory]) {
    if (!/^[0-9a-f]{64}$/.test(entry.artifact_id)
        || entry.content_url !== `/api/v1/executions/${encodeURIComponent(detail.id)}/artifacts/${entry.artifact_id}`) {
      throw new Error("执行结果地址无效");
    }
  }
  return {diff, inventory, key: `${detail.id}:${manifest.manifest_hash}:${diff.artifact_id}:${inventory.artifact_id}`};
}

// One controller per mounted task: immutable artifacts are loaded once; status
// polling never replaces the diff DOM, selection, expanded files or scroll.
export function createExecutionChanges({openDetails = () => {}, onSummaryChange = () => {}} = {}) {
  const summary = node("section", "", "changes-summary");
  const viewer = node("section", "", "changes-viewer");
  const stateLabel = node("p", "", "changes-verification");
  let currentKey = null, currentDetail = null, disposed = false, request = null;
  const fileNodes = new Map();
  const cache = new Map();
  summary.hidden = true;

  function showState(message, retry = false) {
    summary.replaceChildren(node("strong", "代码变更"), node("p", message));
    viewer.replaceChildren(node("h5", "代码变更"), node("p", message), stateLabel);
    if (retry) {
      summary.append(button("重试读取变更", () => update(currentDetail, true)));
      viewer.append(button("重试读取变更", () => update(currentDetail, true)));
    }
    onSummaryChange();
  }
  function select(path) {
    openDetails();
    const entry = fileNodes.get(path);
    if (!entry) return;
    entry.open = true;
    entry.dispatchEvent(new Event("toggle"));
    entry.querySelector("summary").focus({preventScroll: true});
    entry.scrollIntoView({block: "start"});
  }
  function render(model) {
    fileNodes.clear();
    summary.replaceChildren(node("strong", title(model)));
    viewer.replaceChildren(node("h5", title(model)), stateLabel);
    let visibleFiles = 0;
    const summaryList = node("div", "", "changes-file-list");
    const moreFiles = button("显示更多文件", appendFiles);
    function appendFiles() {
      for (const file of model.files.slice(visibleFiles, visibleFiles + 5)) {
        const link = button(`${file.path} · ${labels[file.status]} · ${stats(file)}`, () => select(file.path));
        summaryList.append(link);
      }
      visibleFiles += 5;
      moreFiles.hidden = visibleFiles >= model.files.length;
    }
    appendFiles();
    summary.append(summaryList, moreFiles);
    if (model.malformed) viewer.append(node("p", "无法解析此差异；请查看原始 diff。", "changes-warning"));
    for (const file of model.files) {
      const section = node("details", "", "changes-file");
      section.dataset.path = file.path;
      section.append(node("summary", `${file.path} · ${labels[file.status]} · ${stats(file)}`));
      fileNodes.set(file.path, section);
      let initialized = false;
      section.addEventListener("toggle", () => {
        if (!section.open || initialized) return;
        initialized = true;
        const copyStatus = node("span"); copyStatus.setAttribute("role", "status");
        section.append(button("复制路径", async () => {
          try { await navigator.clipboard.writeText(file.path); copyStatus.textContent = "已复制"; }
          catch { copyStatus.textContent = "复制失败，请手动选择文件路径。"; }
        }), copyStatus);
        if (file.modeChanged) section.append(node("p", "文件执行权限已变化"));
        if (file.unavailable) section.append(node("p", model.malformed ? "文本差异无法解析。" : "二进制或内容未收录，无法展示文本差异。"));
        else if (!file.rows.length) section.append(node("p", "无文本行变化"));
        const scroll = node("div", "", "changes-code-scroll");
        scroll.tabIndex = 0; scroll.setAttribute("role", "region"); scroll.setAttribute("aria-label", `${file.path} 代码差异`);
        const table = node("table", "", "changes-code");
        const head = node("thead"); const heading = node("tr");
        for (const label of ["原行", "新行", "+/−", "代码"]) { const cell = node("th", label); cell.scope = "col"; heading.append(cell); }
        head.append(heading); const body = node("tbody"); table.append(head, body); scroll.append(table);
        let offset = 0;
        const more = button("加载更多差异行", appendRows);
        function appendRows() {
          for (const row of file.rows.slice(offset, offset + PAGE_SIZE)) {
            const tr = node("tr", "", `changes-row changes-${row.kind}`);
            tr.append(node("td", row.oldLine ?? "", "changes-line-number"), node("td", row.newLine ?? "", "changes-line-number"),
              node("td", row.kind === "add" ? "+" : row.kind === "delete" ? "−" : "", "changes-sign"), node("td", row.text, "changes-source"));
            body.append(tr);
          }
          offset += PAGE_SIZE; more.hidden = offset >= file.rows.length;
        }
        if (file.rows.length) { appendRows(); section.append(scroll, more); }
      });
      viewer.append(section);
    }
    const raw = node("details", "", "changes-raw");
    raw.append(node("summary", "查看原始 diff"));
    raw.addEventListener("toggle", () => {
      if (raw.open && raw.childElementCount === 1) raw.append(node("pre", model.raw || "空 diff"));
    });
    viewer.append(raw);
    onSummaryChange();
  }
  async function update(detail, retry = false) {
    if (disposed) return;
    currentDetail = detail;
    const verify = [...(detail?.stage_runs || [])].reverse().find(run => run.stage === "verify");
    const status = {succeeded: "已完成", failed: "失败", cancelled: "已取消", timed_out: "超时", running: "进行中", queued: "等待中", leased: "准备中"};
    stateLabel.textContent = `测试验证：${status[verify?.job?.state] || "尚未完成"}；审查：${detail?.reviews?.length ? "请查看下方审查结果" : "尚未完成"}`;
    let artifacts;
    try { artifacts = implementationArtifacts(detail); }
    catch (error) {
      request?.abort(); currentKey = null; summary.hidden = false;
      showState(error.message, true); return;
    }
    const key = artifacts?.key || `${detail?.id || "none"}:pending`;
    if (key === currentKey && !retry) return;
    currentKey = key; request?.abort();
    fileNodes.clear();
    summary.hidden = !artifacts;
    if (!artifacts) { showState("代码修改结果尚未生成。"); return; }
    if (cache.has(key) && !retry) { render(cache.get(key)); return; }
    showState("正在读取代码变更…");
    const controller = new AbortController(); request = controller;
    try {
      const [diff, inventory] = await Promise.all([
        requestArtifact(artifacts.diff.content_url, {signal: controller.signal}),
        requestArtifact(artifacts.inventory.content_url, {signal: controller.signal}),
      ]);
      if (disposed || currentKey !== key || controller.signal.aborted) return;
      const model = parseExecutionChanges(diff.text, inventory.value);
      cache.set(key, model);
      // Bound task-local memory across automatic repair attempts.
      if (cache.size > 4) cache.delete(cache.keys().next().value);
      render(model);
    } catch (error) {
      if (!disposed && currentKey === key && !controller.signal.aborted) {
        showState("代码变更读取失败，无法确认修改内容。", true);
      }
    }
  }
  return {summary, viewer, update, select,
    fail() {
      if (disposed) return;
      request?.abort(); currentKey = null; cache.clear(); fileNodes.clear();
      summary.hidden = false;
      stateLabel.textContent = "执行结果暂时无法确认";
      showState("执行记录读取失败，正在等待重新读取。");
    },
    dispose() { disposed = true; request?.abort(); cache.clear(); },
  };
}
