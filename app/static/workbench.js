import { requestJSON } from "./api.js?v=workbench-v1";

const terminal = new Set(["succeeded", "failed", "cancelled", "timed_out"]);
const fields = [
  ["goal", "目标与改造说明", false],
  ["acceptance_criteria", "验收标准", true],
  ["files_to_inspect", "已阅读的相关文件", true],
  ["files_likely_to_change", "计划修改的文件", true],
  ["implementation_steps", "实施步骤", true],
  ["tests_to_add_or_run", "测试与验证", true],
  ["risks", "风险与取舍", true],
  ["questions_for_maintainer", "待确认问题", true],
];
function node(tag, text = "", className = "") {
  const el = document.createElement(tag);
  el.textContent = text;
  el.className = className;
  return el;
}
function button(text, handler) {
  const el = node("button", text, "analysis-action");
  el.type = "button";
  el.addEventListener("click", handler);
  return el;
}

export function mountContributionWorkbench(root, taskId, options) {
  const endpoint = `/api/v1/tasks/${encodeURIComponent(taskId)}/workbench`;
  let data = null;
  let plan = null;
  let dirty = false;
  let editing = false;
  let sending = false;
  let timer = null;
  let disposed = false;
  let seenEvents = "";
  let editorPlanId = null;
  let executionFingerprint = "";
  let lastError = "";
  const versionDiffs = new Map();
  const shell = node("section", "", "ai-workbench");
  const headline = node("div", "", "ai-workbench-heading");
  headline.append(node("h3", "贡献方案"));
  const progress = node("p", "正在读取任务…", "ai-workbench-progress");
  progress.setAttribute("role", "status");
  progress.setAttribute("aria-live", "polite");
  const error = node("p", "", "ai-workbench-error");
  error.setAttribute("role", "alert");
  const tabs = node("div", "", "ai-workbench-tabs");
  const layout = node("div", "", "ai-workbench-layout");
  const chat = node("section", "", "ai-workbench-chat");
  const documentPane = node("section", "", "ai-workbench-document");
  for (const [name, target] of [["对话", "chat"], ["方案", "document"]]) {
    tabs.append(button(name, () => { shell.dataset.mobilePane = target; }));
  }
  shell.dataset.mobilePane = "chat";
  chat.append(node("h4", "与 AI 讨论改造方案"));
  const messages = node("div", "", "ai-workbench-messages");
  messages.setAttribute("aria-label", "规划对话记录");
  const chatForm = node("form", "", "ai-workbench-chat-form");
  const input = node("textarea");
  input.rows = 4;
  input.maxLength = 8000;
  input.required = true;
  input.placeholder = "说明目标、回答问题，或让 AI 修改右侧方案…";
  input.setAttribute("aria-label", "发送给规划助手的消息");
  const send = button("发送", () => {});
  send.type = "submit";
  const regenerate = button("重新读取上游代码", () => act(async () => {
    if (dirty) throw new Error("请先保存或撤销方案编辑。");
    await post("/messages", {text: "请重新读取最新代码，根据当前方案重新规划。", ...parent(), refresh: true});
  }));
  const stop = button("停止并返回方案编辑", () => act(() => post("/stop", {})));
  chatForm.append(input, send);
  chatForm.addEventListener("submit", (event) => {
    event.preventDefault();
    act(async () => {
      if (dirty) throw new Error("请先保存方案编辑，再让 AI 修订。");
      const text = input.value.trim();
      if (!text) return;
      await post("/messages", {text, ...parent()});
      input.value = "";
    });
  });
  chat.append(messages, chatForm, regenerate, stop);
  const documentHeading = node("div", "", "ai-plan-heading");
  const version = node("span");
  const edit = button("编辑方案", () => { editing = true; renderDocument(); });
  documentHeading.append(node("h4", "改造方案"), version, edit);
  const editor = node("div", "", "ai-plan-content");
  const changes = node("details", "", "technical-details");
  changes.append(node("summary", "版本与差异"));
  const versionList = node("div");
  changes.append(versionList);
  const confirm = node("label", "", "ai-plan-consent");
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  confirm.append(checkbox, document.createTextNode("我批准当前方案，并启动修改、测试和独立审查；范围内最多自动修复两轮。"));
  const execute = button("确认方案并执行", () => act(async () => {
    if (!checkbox.checked || dirty || !plan || editorPlanId !== plan.id) throw new Error("请保存并确认当前方案。");
    await post("/execute", {plan_id: plan.id, plan_hash: plan.record_hash,
      approve_plan: true, start_execution: true, max_repairs: 2});
    checkbox.checked = false;
  }));
  checkbox.addEventListener("change", controls);
  documentPane.append(documentHeading, editor, changes, confirm, execute);
  layout.append(chat, documentPane);
  const execution = node("section", "", "ai-workbench-execution");
  const publication = node("section", "", "ai-workbench-publication");
  shell.append(headline, progress, error, tabs, layout, execution, publication);
  root.replaceChildren(shell);
  const observer = new MutationObserver(() => {
    if (!shell.isConnected) {
      disposed = true;
      clearTimeout(timer);
      observer.disconnect();
    }
  });
  observer.observe(document.body, {childList: true, subtree: true});

  function parent() { return {parent_id: plan?.id || null, expected_hash: plan?.record_hash || null}; }
  async function post(path, payload) {
    await options.ensureAccess();
    return requestJSON(endpoint + path, {method: "POST",
      headers: options.headers({"Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID()}),
      body: JSON.stringify(payload)});
  }
  async function act(action) {
    if (sending) return;
    sending = true; error.textContent = ""; controls();
    try { await action(); await load(false); }
    catch (err) { error.textContent = err.message; }
    finally { sending = false; controls(); }
  }
  function controls() {
    const planning = data?.state === "planning";
    send.disabled = sending || !planning || data?.busy || dirty || !data?.planner_available;
    regenerate.disabled = send.disabled;
    edit.disabled = !planning || data?.busy || sending || !plan;
    const assistant = data?.events.filter(e => e.kind === "assistant_message").at(-1);
    const questions = assistant?.payload.questions?.length || assistant?.payload.read_paths?.length;
    execute.disabled = sending || !planning || data?.busy || dirty || !plan || editorPlanId !== plan.id || !data?.context || questions || plan.questions_for_maintainer.length || !checkbox.checked;
    confirm.hidden = !planning;
    execute.hidden = !planning;
    stop.disabled = sending || !!data?.events.find(e => e.kind === "publication_confirmed");
  }
  function renderDocument() {
    editor.replaceChildren();
    version.textContent = plan ? `v${plan.version_number}` : "等待 AI 生成";
    edit.hidden = editing;
    if (!plan) {
      editor.append(node("p", "AI 会先阅读相关代码，必要时向您提问，然后在这里给出可编辑的改造方案。"));
      return;
    }
    editorPlanId = plan.id;
    const form = node("form", "", "ai-plan-editor");
    for (const [name, title, list] of fields) {
      const block = node("section");
      block.append(node("h5", title));
      if (editing) {
        const label = node("label");
        label.append(node("span", title, "sr-only"));
        const field = node("textarea");
        field.name = name;
        field.rows = name === "implementation_steps" ? 7 : 3;
        field.value = list ? (plan[name] || []).join("\n") : (plan[name] || "");
        field.setAttribute("aria-label", title);
        field.addEventListener("input", () => { dirty = true; checkbox.checked = false; controls(); });
        label.append(field); block.append(label);
      } else if (list) {
        const ul = node(name === "implementation_steps" ? "ol" : "ul");
        for (const text of plan[name] || []) ul.append(node("li", text));
        block.append(ul.childElementCount ? ul : node("p", "无"));
      } else block.append(node("p", plan[name] || ""));
      form.append(block);
    }
    const advanced = node("details", "", "technical-details");
    advanced.append(node("summary", "验证命令与执行依据"));
    const commands = node(editing ? "textarea" : "pre");
    if (editing) {
      commands.name = "commands_to_run"; commands.rows = 8;
      commands.value = JSON.stringify(plan.commands_to_run, null, 2);
      commands.setAttribute("aria-label", "验证命令 JSON");
      commands.addEventListener("input", () => { dirty = true; checkbox.checked = false; controls(); });
    } else commands.textContent = plan.commands_to_run.map(c => `${c.working_directory}: ${c.argv.join(" ")}`).join("\n");
    advanced.append(commands);
    if (data?.context) advanced.append(node("p", `Base ${data.context.base_sha}\nArchive ${data.context.archive_hash}`));
    form.append(advanced);
    if (editing) {
      const save = button("保存新版本", () => {}); save.type = "submit";
      const cancel = button("撤销编辑", () => { dirty = false; editing = false; renderDocument(); controls(); });
      form.append(save, cancel);
      form.addEventListener("submit", event => {
        event.preventDefault();
        act(async () => {
          const payload = {parent_version_id: editorPlanId};
          for (const [name, , list] of fields) {
            const value = form.elements.namedItem(name).value.trim();
            payload[name] = list ? value.split("\n").map(v => v.trim()).filter(Boolean) : value;
          }
          payload.commands_to_run = JSON.parse(form.elements.namedItem("commands_to_run").value);
          await post("/plans", {context_hash: data.context.record_hash, plan: payload});
          dirty = false; editing = false; editorPlanId = null;
        });
      });
    }
    editor.append(form);
  }
  function renderMessages() {
    const signature = data.events.map(e => e.id).join(":");
    if (signature === seenEvents) return;
    seenEvents = signature;
    messages.replaceChildren();
    for (const entry of data.events) {
      if (!["user_message", "assistant_message"].includes(entry.kind)) continue;
      const item = node("article", "", `ai-message ${entry.kind}`);
      item.append(node("strong", entry.kind === "user_message" ? "您" : "规划助手"));
      item.append(node("p", entry.payload.text || entry.payload.reply));
      for (const question of entry.payload.questions || []) {
        item.append(node("p", question.prompt));
        for (const option of question.options || []) {
          item.append(button(option, () => { input.value += `${input.value ? "\n" : ""}${question.prompt}：${option}`; input.focus(); }));
        }
      }
      if (entry.payload.read_paths?.length) item.append(node("p", "继续阅读：" + entry.payload.read_paths.join("、")));
      messages.append(item);
    }
  }
  async function load(first = false) {
    clearTimeout(timer);
    if (disposed) return;
    try {
      data = await requestJSON(endpoint);
      if (disposed) return;
      const nextPlan = data.plans.at(-1) || null;
      if (plan?.id !== nextPlan?.id) {
        checkbox.checked = false;
        if (editing || dirty) error.textContent = "方案已有新版本。当前编辑已保留，请撤销编辑以载入最新版本。";
      }
      plan = nextPlan;
      if ((!editing && !dirty && editorPlanId !== plan?.id) || first) renderDocument();
      renderMessages();
      versionList.replaceChildren();
      for (const versionPlan of data.plans) {
        const row = node("p", `v${versionPlan.version_number} · ${versionPlan.goal.slice(0, 100)}`);
        if (versionPlan.parent_version_id) row.append(button("查看与上一版差异", () => act(async () => {
          const query = new URLSearchParams({left_version_id: versionPlan.parent_version_id, right_version_id: versionPlan.id});
          const diff = await requestJSON(`/api/v1/tasks/${encodeURIComponent(taskId)}/plan-versions/compare?${query}`);
          versionDiffs.set(versionPlan.id, diff.unified_diff);
        })));
        if (versionDiffs.has(versionPlan.id)) row.append(node("pre", versionDiffs.get(versionPlan.id), "ai-plan-diff"));
        versionList.append(row);
      }
      const active = data.jobs.filter(j => !terminal.has(j.state));
      const names = {planning_archive: "正在获取仓库版本", planning_context: "正在阅读代码", planning_turn: "AI 正在制定方案",
        contribution_start: "正在检查并启动执行", coding_context: "正在读取实施上下文", coding_turn: "正在分析实施步骤",
        change_set_proposal: "正在生成代码修改", sandbox_stage: "正在隔离执行与测试", provider_review: "正在独立审查",
        publication_prepare: "正在准备发布预览", publication_write: "正在发布并核对远端状态"};
      const blocked = data.events.filter(e => e.kind === "automation_blocked").at(-1);
      progress.textContent = active.length ? (names[active[0].kind] || "任务正在运行")
        : data.state === "ready" ? "验证与审查已完成，等待您验收并确认发布。"
        : data.state === "planning" ? "讨论并完善方案，确认后即可执行。"
        : blocked ? blocked.payload.reason : "正在推进贡献流程…";
      const latestJob = data.jobs.at(-1);
      const failed = !active.length && ["failed", "timed_out"].includes(latestJob?.state) ? latestJob : null;
      if (failed && failed.id !== lastError) { lastError = failed.id; error.textContent = failed.error_message || "任务失败，请检查后重试。"; }
      if (!failed && lastError) {
        const previousError = data.jobs.find(j => j.id === lastError);
        if (error.textContent === (previousError?.error_message || "任务失败，请检查后重试。")) error.textContent = "";
        lastError = "";
      }
      if (!data.planner_available) error.textContent = data.planner_unavailable_reason || "尚未配置规划模型服务。";
      const task = await requestJSON(`/api/v1/tasks/${encodeURIComponent(taskId)}`);
      if (task.latest_execution_attempt_id) {
        const detail = await requestJSON(`/api/v1/executions/${encodeURIComponent(task.latest_execution_attempt_id)}`);
        const signature = JSON.stringify(detail);
        if (!disposed && signature !== executionFingerprint) {
          executionFingerprint = signature;
          options.renderExecution(execution, detail, plan, () => load(false));
        }
      }
      renderPublication();
      controls();
      if (first && !data.events.length && data.planner_available && data.state === "planning") {
        await act(() => post("/messages", {...parent()}));
      }
    } catch (err) { if (!disposed) error.textContent = err.message; }
    finally { clearTimeout(timer); if (!disposed) timer = setTimeout(() => load(false), 3000); }
  }

  let publicationVersion = "";
  function renderPublication() {
    const prepared = data.events.filter(e => e.kind === "publication_prepared").at(-1);
    const completed = data.events.filter(e => e.kind === "publication_completed").at(-1);
    const confirmed = data.events.find(e => e.kind === "publication_confirmed");
    const write = data.jobs.find(j => j.kind === "publication_write");
    const signature = `${data.state}:${prepared?.id}:${completed?.id}:${confirmed?.id}:${write?.state}:${write?.attempt_count}`;
    if (signature === publicationVersion) return;
    publicationVersion = signature;
    publication.replaceChildren();
    if (completed) {
      const link = node("a", "查看 Draft PR");
      if (/^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/pull\/\d+$/.test(completed.payload.html_url)) {
        link.href = completed.payload.html_url; link.target = "_blank"; link.rel = "noopener noreferrer";
      }
      publication.append(node("h4", "Draft PR 已创建"), link); return;
    }
    if (data.state !== "ready") return;
    if (confirmed) {
      publication.append(node("h4", "发布已确认"), node("p", "Publisher 将核对已确认的远端分支与 Draft PR。"));
      if (write && ["failed", "timed_out"].includes(write.state)) {
        publication.append(button("重试核对发布结果", () => act(async () => {
          await options.ensureAccess();
          await requestJSON(`/api/v1/jobs/${encodeURIComponent(write.id)}/retry`, {method: "POST", headers: options.headers()});
        })));
      }
      return;
    }
    publication.append(node("h4", "人工验收与发布"), node("p", "请检查上方 diff、测试和审查结果。最终确认后才会 Fork、Push 并创建 Draft PR。"));
    if (data.publisher_mode !== "gh") {
      publication.append(node("p", "真实发布尚未配置。请按部署文档配置独立 Publisher；当前结果和方案会保留。")); return;
    }
    const title = node("input"); title.setAttribute("aria-label", "PR 标题"); title.maxLength = 200;
    title.value = prepared?.payload.title || (plan?.goal || "Contribution").split("\n")[0].slice(0, 200);
    const body = node("textarea"); body.rows = 8; body.setAttribute("aria-label", "PR 正文");
    body.value = prepared?.payload.body || `## Changes\n${(plan?.implementation_steps || []).map(v => "- " + v).join("\n")}\n\n## Validation\n${(plan?.tests_to_add_or_run || []).map(v => "- " + v).join("\n")}`;
    publication.append(title, body, button("准备发布预览", () => act(() => post("/publication", {title: title.value, body: body.value}))));
    if (prepared) {
      const preview = node("pre", JSON.stringify(prepared.payload, null, 2));
      const technical = node("details", "", "technical-details"); technical.append(node("summary", "完整发布内容与校验信息"), preview);
      publication.append(node("p", `${prepared.payload.repository} ← ${prepared.payload.fork}:${prepared.payload.branch}`), technical);
      const consent = node("label"); const checked = document.createElement("input"); checked.type = "checkbox";
      consent.append(checked, document.createTextNode("我已验收代码和测试，确认发布上述精确内容。"));
      const publish = button("确认 Fork、Push 并创建 Draft PR", () => act(async () => {
        if (!checked.checked || title.value !== prepared.payload.title || body.value !== prepared.payload.body) throw new Error("内容已编辑，请重新准备预览并确认。");
        await post("/publication/confirm", {intent_hash: prepared.record_hash, nonce: prepared.payload.nonce});
      }));
      publish.disabled = true;
      checked.addEventListener("change", () => { publish.disabled = !checked.checked; });
      publication.append(consent, publish);
    }
  }
  load(true);
}
