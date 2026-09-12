import { requestJSON } from "./api.js?v=workbench-v1";
import { renderMarkdown } from "./studio.js?v=studio-v1";
import { createExecutionChanges } from "./execution-changes.js?v=execution-changes-v1";

const drafts = new Map();

const terminal = new Set(["succeeded", "failed", "cancelled", "timed_out"]);
function jobErrorMessage(job) {
  return job?.state === "cancelled" ? "本次请求已取消，未继续处理。你可以重新发送消息。"
    : job?.error_message || "任务失败，请检查后重试。";
}
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
  let clockTimer = null;
  let disposed = false;
  let seenEvents = "";
  let editorPlanId = null;
  let executionFingerprint = "";
  let executionReadError = "";
  let executionId = null;
  let panelOpener = null;
  let lastError = "";
  let modelData = {profiles:[]};
  let modelFieldsDirty = false;
  let modelSelectionHash = null;
  const versionDiffs = new Map();
  const shell = node("section", "", "ai-workbench");
  const headline = node("div", "", "ai-workbench-heading");
  headline.append(node("h3", "贡献方案"));
  const progress = node("p", "正在读取任务…", "ai-workbench-progress");
  progress.setAttribute("role", "status");
  progress.setAttribute("aria-live", "polite");
  const progressLabel = node("span", "正在读取任务…");
  const elapsed = node("span");
  elapsed.setAttribute("aria-hidden", "true");
  progress.replaceChildren(progressLabel, elapsed);
  const error = node("p", "", "ai-workbench-error");
  error.setAttribute("role", "alert");
  const layout = node("div", "", "ai-workbench-layout");
  const chat = node("section", "", "ai-workbench-chat");
  const documentPane = node("section", "", "ai-workbench-document");
  const paneHeader = node("div", "", "studio-pane-header");
  const paneTitle = node("h4", "贡献方案");
  paneTitle.tabIndex = -1;
  paneHeader.append(paneTitle, button("返回对话", () => showPane("chat")));
  documentPane.append(paneHeader);
  function showPane(target, focus = true) {
    if(target !== "chat") panelOpener = document.activeElement;
    shell.dataset.mobilePane = target; shell.dataset.panel = target;
    paneTitle.textContent = target === "results" ? "代码、测试与审查" : "贡献方案";
    if(focus) {
      if(target !== "chat") paneTitle.focus();
      else if(panelOpener?.isConnected && panelOpener.getClientRects().length) panelOpener.focus();
      else input.focus();
    }
  }
  showPane("chat", false);
  chat.append(node("h4", "与 AI 讨论改造方案"));
  const messages = node("div", "", "ai-workbench-messages");
  messages.setAttribute("aria-label", "规划对话记录");
  messages.append(progress);
  const chatForm = node("form", "", "ai-workbench-chat-form");
  const input = node("textarea");
  input.rows = 4;
  input.maxLength = 8000;
  input.required = true;
  input.placeholder = "说明目标、回答问题，或直接告诉 AI 怎么修改方案…";
  input.setAttribute("aria-label", "发送给规划助手的消息");
  input.value = drafts.get(taskId)?.text || "";
  const modelSelect = node("select"); modelSelect.setAttribute("aria-label","方案讨论模型");
  modelSelect.append(Object.assign(node("option","当前任务模型"),{value:""}));
  modelSelect.addEventListener("change",()=>{ checkbox.checked=false; error.textContent="新模型将在下一条消息生效；之后需要重新确认方案。"; });
  input.addEventListener("input",()=>drafts.set(taskId,{...drafts.get(taskId),text:input.value}));
  input.addEventListener("keydown",event=>{if(event.key==="Enter" && (event.metaKey || event.ctrlKey) && !event.isComposing){event.preventDefault();chatForm.requestSubmit();}});
  const send = button("发送", () => {});
  send.type = "submit";
  const regenerate = button("重新读取上游代码", () => act(async () => {
    more.open = false;
    if (dirty) throw new Error("请先保存或撤销方案编辑。");
    await post("/messages", {text: "请重新读取最新代码，根据当前方案重新规划。", ...parent(), refresh: true});
  }));
  const stopAction = () => act(async () => { more.open=false; await post("/stop", {}); });
  const stop = button("停止", stopAction);
  stop.title = "停止当前处理，保留已有方案和上下文";
  const paneStop = button("停止", stopAction);
  paneStop.hidden = stop.hidden = true;
  paneHeader.append(paneStop);
  const more = node("details", "", "studio-composer-more");
  const moreToggle = node("summary", "更多");
  moreToggle.setAttribute("aria-label", "更多任务操作");
  const resumePlanning = button("返回方案讨论", stopAction);
  resumePlanning.hidden = true;
  const moreActions = node("div", "", "studio-more-actions");
  const modelSettings = button("任务模型设置", () => {
    more.open=false; showPane("document"); taskModels.open=true;
    taskModels.querySelector("summary").focus();
  });
  const archive = button("归档任务", () => act(async () => {
    if (dirty || input.value.trim()) throw new Error("请先保存方案编辑，并发送或清空未发送的消息。");
    more.open = false;
    await options.onArchive();
  }));
  archive.hidden = !options.onArchive;
  moreActions.append(regenerate, modelSettings, resumePlanning, archive);
  more.append(moreToggle, moreActions);
  more.addEventListener("keydown", event => { if(event.key === "Escape") { more.open=false; moreToggle.focus(); event.stopPropagation(); } });
  const composerTools=node("div","","studio-composer-tools"); composerTools.append(more,modelSelect,send,stop);
  chatForm.append(input, composerTools);
  chatForm.addEventListener("submit", (event) => {
    event.preventDefault();
    act(async () => {
      if (dirty) throw new Error("请先保存方案编辑，再让 AI 修订。");
      const text = input.value.trim();
      if (!text) return;
      await post("/messages", {text, ...parent(), model_profile_id:modelSelect.value || null});
      input.value = "";
      drafts.delete(taskId);
    });
  });
  chat.append(messages, chatForm);
  const documentHeading = node("div", "", "ai-plan-heading");
  const version = node("span");
  const edit = button("编辑方案", () => { editing = true; renderDocument(); });
  documentHeading.append(node("h4", "改造方案"), version, edit);
  const editor = node("div", "", "ai-plan-content");
  const changes = node("details", "", "technical-details");
  changes.append(node("summary", "版本与差异"));
  changes.classList.add("studio-plan-versions");
  const versionList = node("div");
  changes.append(versionList);
  const confirm = node("label", "", "ai-plan-consent");
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  confirm.append(checkbox, document.createTextNode("我批准当前方案，并启动修改、测试和独立审查；范围内最多自动修复两轮。"));
  const execute = button("确认方案并执行", () => act(async () => {
    if (!checkbox.checked || dirty || !plan || editorPlanId !== plan.id) throw new Error("请保存并确认当前方案。");
    await post("/execute", {plan_id: plan.id, plan_hash: plan.record_hash,
      approve_plan: true, start_execution: true, max_repairs: 2, model_binding_hash:data.model_binding_hash});
    showPane("results");
    checkbox.checked = false;
  }));
  checkbox.addEventListener("change", controls);
  execute.classList.add("studio-plan-execute");
  const modelSummary=node("p","","muted");
  const taskModels=node("details","","studio-task-models");taskModels.append(node("summary","本任务的模型选择"));
  const taskModelFields=node("div"); taskModels.append(taskModelFields);
  const modelSelectors={};
  const applyModels=button("应用到此任务",()=>act(async()=>{
    if(dirty)throw new Error("请先保存当前方案编辑。");
    const result = await post("/models",{expected_hash:modelSelectionHash,profiles:Object.fromEntries(Object.entries(modelSelectors).map(([stage,select])=>[stage,select.value || null]))});
    modelFieldsDirty=false;
    if(result.changed) { checkbox.checked=false;error.textContent="模型已更新，请重新保存方案或发送消息生成新版本后再执行。"; }
    else error.textContent="模型选择未变化，无需重新保存方案。";
  }));taskModels.append(applyModels);
  documentPane.append(documentHeading, editor, changes, confirm, execute);
  documentPane.insertBefore(modelSummary,confirm);
  documentPane.insertBefore(taskModels,confirm);
  layout.append(chat, documentPane);
  const execution = node("section", "", "ai-workbench-execution");
  const codeChanges = createExecutionChanges({
    openDetails: () => showPane("results"),
    onSummaryChange: () => {
      if (disposed) return;
      const atBottom = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 100;
      if (atBottom) messages.scrollTop = messages.scrollHeight;
    },
  });
  const codeChangesPane = node("section", "", "ai-workbench-execution");
  codeChangesPane.append(codeChanges.viewer);
  const publication = node("section", "", "ai-workbench-publication");
  documentPane.append(codeChangesPane,execution,publication);
  shell.append(headline, error, layout);
  root.replaceChildren(shell);
  const observer = new MutationObserver(() => {
    if (!shell.isConnected) {
      dispose();
      observer.disconnect();
    }
  });
  observer.observe(document.body, {childList: true, subtree: true});
  function persistDraft() {
    const values={};
    if(editing) for(const field of editor.querySelectorAll("textarea")) values[field.name]=field.value;
    drafts.set(taskId,{text:input.value,values,dirty,editing,editorPlanId});
  }
  function dispose() { if(disposed)return;persistDraft();disposed=true;codeChanges.dispose();clearTimeout(timer);clearInterval(clockTimer);observer.disconnect();window.removeEventListener("beforeunload",warnUnload); }
  function warnUnload(event) { if(dirty || input.value.trim()) { event.preventDefault();event.returnValue=""; } }
  window.addEventListener("beforeunload",warnUnload);

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
    archive.disabled = sending || !data || data.busy;
    const planning = data?.state === "planning";
    send.disabled = sending || !planning || data?.busy || dirty || !data?.planner_available;
    send.hidden = !!data?.busy;
    modelSelect.disabled = sending || data?.busy || !planning || dirty;
    const modelsChanged = Object.entries(modelSelectors).some(([stage,select]) =>
      (select.value || null) !== (data?.model_profiles?.[stage] || null));
    applyModels.disabled = sending || data?.busy || !planning || dirty || Object.keys(modelSelectors).length !== 4 || !modelsChanged;
    regenerate.disabled = send.disabled;
    edit.disabled = !planning || data?.busy || sending || !plan;
    const assistant = data?.events.filter(e => e.kind === "assistant_message").at(-1);
    const questions = assistant?.payload.questions?.length || assistant?.payload.read_paths?.length;
    execute.disabled = sending || !planning || data?.busy || dirty || modelsChanged || !plan || editorPlanId !== plan.id || !data?.context || questions || plan.questions_for_maintainer.length || !checkbox.checked;
    confirm.hidden = !planning;
    execute.hidden = !planning;
    const publishing = !!data?.events.find(e => e.kind === "publication_confirmed");
    stop.hidden = paneStop.hidden = !data?.busy || publishing;
    stop.disabled = paneStop.disabled = sending;
    resumePlanning.hidden = !data || planning || data.busy || publishing;
    resumePlanning.disabled = sending;
  }
  function renderDocument() {
    editor.replaceChildren();
    version.textContent = plan ? `v${plan.version_number}` : "等待 AI 生成";
    edit.hidden = editing;
    if (!plan) {
      editor.append(node("p", "发送目标后，AI 会阅读代码、提出问题并生成可编辑的方案。"));
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
      const cancel = button("撤销编辑", () => { dirty = false; editing = false; drafts.delete(taskId); renderDocument(); controls(); });
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
    const signature = data.events.map(e => e.id).join(":") + ":" + (plan?.id || "") + ":" + (executionId || "") + ":" + data.state;
    if (signature === seenEvents) return;
    seenEvents = signature;
    const atBottom=messages.scrollHeight-messages.scrollTop-messages.clientHeight<100;
    const previousScroll=messages.scrollTop;
    messages.replaceChildren();
    if(!data.events.length) { const welcome=node("article","","ai-message");welcome.append(node("h2","从这个贡献开始"),node("p","告诉 AI 你想解决什么，它会阅读项目代码，和你一起完善方案。"));welcome.append(button("阅读代码并制定方案",()=>{input.value="请阅读相关代码，为这个 Issue 制定具体改造方案。";chatForm.requestSubmit();}));messages.append(welcome); }
    for (const entry of data.events) {
      if (!["user_message", "assistant_message"].includes(entry.kind)) continue;
      const item = node("article", "", `ai-message ${entry.kind}`);
      if (entry.kind === "assistant_message") item.append(node("strong", "规划助手"));
      const displayText = entry.payload.text || entry.payload.reply || (entry.payload.plan_id ? "方案已生成，可展开查看。" : "");
      const content=node("div");renderMarkdown(content,displayText);item.append(content);
      if(displayText) item.append(button("复制",async()=>{try{await navigator.clipboard.writeText(displayText);}catch{error.textContent="浏览器未允许复制，请手动选择文本。";}}));
      for (const question of entry.payload.questions || []) {
        item.append(node("p", question.prompt));
        for (const option of question.options || []) {
          item.append(button(option, () => { input.value += `${input.value ? "\n" : ""}${question.prompt}：${option}`; input.focus(); }));
        }
      }
      if (entry.payload.read_paths?.length) item.append(node("p", "继续阅读：" + entry.payload.read_paths.join("、")));
      messages.append(item);
    }
    if(plan) { const card=button(`贡献方案 · v${plan.version_number}\n${plan.goal.slice(0,160)}\n查看、编辑并确认执行 →`,()=>showPane("document"));card.className="studio-plan-card";messages.append(card); }
    if(executionId || data.events.some(e => ["automation_started", "awaiting_acceptance", "automation_blocked", "publication_completed"].includes(e.kind))) {
      const card = button("执行记录\n查看代码变更、测试与审查 →", () => showPane("results"));
      card.className = "studio-plan-card studio-results-card";
      messages.append(card);
    }
    messages.append(codeChanges.summary, progress);
    messages.scrollTop=atBottom ? messages.scrollHeight : previousScroll;
  }
  function renderProgress() {
    if (disposed || !data) return;
    const active = data.jobs.find(j => !terminal.has(j.state));
    const names = {planning_archive: "正在获取仓库版本", planning_context: "正在阅读代码", planning_turn: "AI 正在制定方案",
      contribution_start: "正在检查并启动执行", coding_context: "正在读取实施上下文", coding_turn: "正在分析实施步骤",
      change_set_proposal: "正在生成代码修改", sandbox_stage: "正在隔离执行与测试", provider_review: "正在独立审查",
      publication_prepare: "正在准备发布预览", publication_write: "正在发布并核对远端状态"};
    const blocked = data.events.filter(e => e.kind === "automation_blocked").at(-1);
    const label = active ? names[active.kind] || "任务正在运行"
      : data.state === "ready" ? "验证与审查已完成，等待验收并确认发布。"
      : data.jobs.at(-1)?.state === "cancelled" ? "本次请求已取消 · 可重新发送消息"
      : data.state === "planning" ? "讨论并完善方案，确认后即可执行。"
      : blocked ? blocked.payload.reason : "正在推进贡献流程…";
    if(progressLabel.textContent !== label) progressLabel.textContent = label;
    const started = active && Date.parse(active.started_at || active.created_at);
    elapsed.textContent = active && Number.isFinite(started) ? ` · ${Math.max(0, Math.floor((Date.now()-started)/1000))} 秒` : "";
  }
  clockTimer = setInterval(renderProgress, 1000);
  async function load(first = false) {
    clearTimeout(timer);
    if (disposed) return;
    try {
      const nextData = await requestJSON(endpoint);
      if(data?.model_binding_hash !== nextData.model_binding_hash) checkbox.checked=false;
      data=nextData;
      if (disposed) return;
      const nextPlan = data.plans.at(-1) || null;
      if (plan?.id !== nextPlan?.id) {
        checkbox.checked = false;
        if (editing || dirty) error.textContent = "方案已有新版本。当前编辑已保留，请撤销编辑以载入最新版本。";
      }
      plan = nextPlan;
      if(first && drafts.get(taskId)?.editing && drafts.get(taskId)?.editorPlanId === plan?.id) {
        const draft=drafts.get(taskId);editing=true;dirty=draft.dirty;renderDocument();
        for(const field of editor.querySelectorAll("textarea")) if(draft.values[field.name]!==undefined) field.value=draft.values[field.name];
      }
      if ((!editing && !dirty && editorPlanId !== plan?.id) || (first && !editing)) renderDocument();
      const modelName=stage=>data.model_labels?.[stage] || modelData.profiles.find(p=>p.id===data.model_profiles?.[stage])?.payload.model || "部署默认模型";
      for(const [stage,id] of Object.entries(data.model_profiles || {})) {
        for(const select of [modelSelectors[stage],stage==="planning" ? modelSelect : null].filter(Boolean)) {
          if(id && ![...select.options].some(option=>option.value===id))select.append(Object.assign(node("option",`${modelName(stage)} · 已绑定版本`),{value:id}));
        }
        if(!modelFieldsDirty && modelSelectors[stage]) modelSelectors[stage].value = id || "";
      }
      if(!modelFieldsDirty) modelSelectionHash = data.model_binding_hash;
      modelSummary.textContent=`实现：${modelName("implementation")} · 审查：${modelName("review")}`;
      if(!modelSelect.value && data.model_profiles?.planning)modelSelect.value=data.model_profiles.planning;
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
      const latestJob = data.jobs.at(-1);
      renderProgress();
      const failed = !active.length && ["failed", "timed_out", "cancelled"].includes(latestJob?.state) ? latestJob : null;
      if (failed && failed.id !== lastError) { lastError = failed.id; error.textContent = jobErrorMessage(failed); }
      if (!failed && lastError) {
        const previousError = data.jobs.find(j => j.id === lastError);
        if (error.textContent === jobErrorMessage(previousError)) error.textContent = "";
        lastError = "";
      }
      if (!data.planner_available) error.textContent = data.planner_unavailable_reason || "尚未配置规划模型服务。";
      const task = await requestJSON(`/api/v1/tasks/${encodeURIComponent(taskId)}`);
      if(disposed) return;
      executionId = task.latest_execution_attempt_id || null;
      renderMessages();
      if (task.latest_execution_attempt_id) {
        const detail = await requestJSON(`/api/v1/executions/${encodeURIComponent(task.latest_execution_attempt_id)}`)
          .catch(error => { executionReadError = error.message; codeChanges.fail(); throw error; });
        if (disposed) return;
        if (executionReadError && error.textContent === executionReadError) error.textContent = "";
        executionReadError = "";
        codeChanges.update(detail);
        const signature = JSON.stringify(detail);
        if (!disposed && signature !== executionFingerprint) {
          executionFingerprint = signature;
          options.renderExecution(execution, detail, plan, () => load(false));
        }
      } else {
        codeChanges.update(null);
        execution.replaceChildren(node("p", "执行尚未产生代码、测试或审查记录。"));
      }
      renderPublication();
      controls();
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
  requestJSON("/api/v1/model-settings").then(result=>{
    if(disposed)return;modelData=result;
    for(const profile of result.profiles)modelSelect.append(Object.assign(node("option",profile.payload.name),{value:profile.id}));
    for(const [stage,title] of [["analysis","分析"],["planning","讨论"],["implementation","实现"],["review","审查"]]) {
      const label=node("label",title),select=node("select");select.setAttribute("aria-label",`本任务${title}模型`);
      select.append(Object.assign(node("option","部署默认"),{value:""}));
      for(const profile of result.profiles)select.append(Object.assign(node("option",profile.payload.name),{value:profile.id}));
      const selected=data ? data.model_profiles?.[stage] || "" : result.effective[stage] || "";
      if(selected && ![...select.options].some(option=>option.value===selected))select.append(Object.assign(node("option",`${data?.model_labels?.[stage] || "历史模型"} · 已绑定版本`),{value:selected}));
      select.value=selected;
      select.addEventListener("change",()=>{modelFieldsDirty=true;controls();});
      label.append(select);taskModelFields.append(label);modelSelectors[stage]=select;
    }
    if(data?.model_profiles?.planning)modelSelect.value=data.model_profiles.planning;
    if(data) modelSelectionHash=data.model_binding_hash;
    controls();
  }).catch(()=>{});
  load(true);
  return {dispose};
}
