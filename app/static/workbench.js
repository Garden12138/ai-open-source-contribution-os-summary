import { requestJSON } from "./api.js?v=workbench-v1";
import { renderMarkdown } from "./studio.js?v=studio-v1";
import { createExecutionChanges } from "./execution-changes.js?v=conversation-v2";

const receiptLabels = {queued:"已收到，等待处理", processing:"正在处理", paused:"已保存，等待继续", completed:"已处理", failed:"暂未完成，内容已保留"};
const planFields = [["goal","要完成的工作"],["files_likely_to_change","修改范围"],
  ["implementation_steps","实施步骤"],["tests_to_add_or_run","测试安排"],
  ["acceptance_criteria","完成标准"],["risks","需要留意"],["questions_for_maintainer","待确认的问题"]];
function node(tag, text = "", className = "") {
  const el = document.createElement(tag); el.textContent = text; el.className = className; return el;
}
function button(text, handler, primary = false) {
  const el = node("button", text, primary ? "analysis-action" : "conversation-link");
  el.type = "button"; el.addEventListener("click", handler); return el;
}
function friendly(text) {
  return String(text || "").replace(/\b(?:sha256:)?[a-f0-9]{40,64}\b/g, "（内部校验信息）")
    .replace(/\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b/g, "（内部记录）")
    .replaceAll("哈希", "校验信息").replaceAll("修改前基线", "修改前测试").replaceAll("基线", "修改前测试")
    .replaceAll("沙箱 Worker", "隔离执行服务").replaceAll("Publisher", "发布服务");
}
function list(root, values) {
  if (!values?.length) return;
  const ul = node("ul"); for (const value of values) ul.append(node("li", friendly(value))); root.append(ul);
}
function safeLink(text, url) {
  const link = node("a", text);
  if (/^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\//.test(url || "")) {
    link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer";
  }
  return link;
}

export function mountContributionWorkbench(root, taskId, options) {
  const endpoint = `/api/v1/tasks/${encodeURIComponent(taskId)}/workbench`;
  const draftKey = `contribos:conversation-draft:${taskId}`;
  let data, timer, disposed = false, sending = false, loading = null, composing = false;
  let requestAttempt, returnFocus, drawerGeneration = 0, changes;
  const entries = new Map();
  const shell = node("section", "", "conversation-agent");
  const header = node("header", "", "conversation-agent-header");
  const status = node("div", "正在读取任务…", "conversation-agent-status");
  status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
  const history = button("历史记录", () => showHistory());
  header.append(status, history);
  const error = node("p", "", "conversation-agent-error"); error.setAttribute("role", "alert");
  const messages = node("div", "", "conversation-agent-messages");
  messages.setAttribute("role", "region"); messages.setAttribute("aria-label", "贡献对话记录"); messages.tabIndex = 0;
  const fresh = button("有新消息 ↓", () => { messages.scrollTop = messages.scrollHeight; fresh.hidden = true; });
  fresh.classList.add("conversation-new-messages"); fresh.hidden = true;
  const form = node("form", "", "conversation-agent-composer");
  const input = node("textarea"); input.rows = 2; input.maxLength = 8000; input.required = true;
  input.placeholder = "说明你的要求，也可以随时补充信息…";
  input.setAttribute("aria-label", "发送给贡献助手的消息");
  try { input.value = sessionStorage.getItem(draftKey) || ""; } catch { /* Storage may be unavailable. */ }
  const toolbar = node("div", "", "conversation-agent-toolbar");
  const hint = node("span", "Enter 发送 · Shift+Enter 换行", "conversation-agent-hint");
  const stop = button("停止", () => act(() => post("/conversation/messages", {text:"停止"}))); stop.hidden = true;
  const send = button("发送 ↑", () => {}, true); send.type = "submit"; send.setAttribute("aria-label", "发送");
  toolbar.append(hint, stop, send); form.append(input, toolbar);
  shell.append(header, error, messages, fresh, form); root.replaceChildren(shell);
  const viewport = window.visualViewport;
  function fitKeyboard() {
    if (!viewport || viewport.scale !== 1) return;
    shell.style.height = document.activeElement === input
      ? `${Math.max(180, Math.min(root.clientHeight, viewport.height + viewport.offsetTop - shell.getBoundingClientRect().top))}px` : "";
  }
  viewport?.addEventListener("resize", fitKeyboard);
  input.addEventListener("focus", fitKeyboard); input.addEventListener("blur", fitKeyboard);

  // One detail surface: native dialog provides Escape, focus trapping and an
  // inert background. Closing preserves the conversation position and focus.
  const drawer = node("dialog", "", "conversation-agent-drawer");
  const drawerHeader = node("header"); const drawerTitle = node("h2", "详情");
  drawerTitle.id = `conversation-detail-${taskId}`; drawer.setAttribute("aria-labelledby", drawerTitle.id);
  const close = button("返回对话", () => drawer.close()); drawerHeader.append(drawerTitle, close);
  const drawerBody = node("div", "", "conversation-agent-drawer-body");
  const drawerError = node("p", "", "conversation-agent-error"); drawerError.setAttribute("role", "alert");
  drawer.append(drawerHeader, drawerError, drawerBody); shell.append(drawer);
  drawer.addEventListener("close", () => { drawerGeneration++; changes?.dispose(); changes = null; (returnFocus?.isConnected ? returnFocus : input).focus({preventScroll:true}); });
  function openDrawer(title) {
    drawerGeneration++; changes?.dispose(); changes = null;
    if (!drawer.open) returnFocus = document.activeElement;
    drawerTitle.textContent = title; drawerError.textContent = ""; drawerBody.replaceChildren();
    if (!drawer.open) drawer.showModal(); drawerBody.scrollTop = 0; close.focus();
    return drawerGeneration;
  }
  function persistDraft() {
    try { input.value ? sessionStorage.setItem(draftKey, input.value) : sessionStorage.removeItem(draftKey); } catch { /* Input remains in memory. */ }
  }
  input.addEventListener("input", () => { persistDraft(); controls(); });
  input.addEventListener("compositionstart", () => { composing = true; });
  input.addEventListener("compositionend", () => { composing = false; });
  input.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && !composing && event.keyCode !== 229) {
      event.preventDefault(); if (!send.disabled) form.requestSubmit();
    }
  });
  messages.addEventListener("scroll", () => { if (atBottom()) fresh.hidden = true; });
  function atBottom() { return messages.scrollHeight - messages.clientHeight - messages.scrollTop < 80; }
  function controls() {
    send.disabled = sending || !input.value.trim() || !data;
    stop.hidden = !data?.conversation?.user_state?.can_stop; stop.disabled = sending;
    for (const el of shell.querySelectorAll("[data-start-planning]")) el.disabled = sending || !!data?.busy;
    for (const el of shell.querySelectorAll("[data-confirmation]")) {
      el.disabled = sending || !!data?.conversation?.pending_count || !!data?.conversation?.paused || !!data?.busy || el.dataset.valid !== "true" || (el.validate && !el.validate());
    }
    for (const el of drawerBody.querySelectorAll("[data-preview-status]")) el.update();
    for (const entry of entries.values()) entry.updateQuestions?.();
  }
  async function post(path, body, key = crypto.randomUUID()) {
    await options.ensureAccess();
    return requestJSON(endpoint + path, {method:"POST", headers:{...options.headers(), "Content-Type":"application/json", "Idempotency-Key":key}, body:JSON.stringify(body)});
  }
  async function act(action) {
    if (sending) return;
    sending = true; error.textContent = ""; drawerError.textContent = ""; controls();
    try { await action(); await load(true); }
    catch (problem) { error.textContent = friendly(problem.message); if (drawer.open) drawerError.textContent = error.textContent; await load(true); }
    finally { sending = false; controls(); }
  }
  form.addEventListener("submit", event => {
    event.preventDefault(); const text = input.value.trim(); if (!text || sending) return;
    if (!requestAttempt || requestAttempt.text !== text) requestAttempt = {text, key:crypto.randomUUID()};
    const attempt = requestAttempt;
    act(async () => {
      await post("/conversation/messages", {text}, attempt.key);
      if (input.value.trim() === text) { input.value = ""; persistDraft(); }
      requestAttempt = null;
    });
  });
  function latest(kind) { return data?.events?.filter(e => e.kind === kind).at(-1); }
  function questionsCurrent(item) {
    const c = data?.conversation;
    return c?.pending_action?.kind === "answer" && c.pending_action.source_id === item.id
      && latest("assistant_message")?.id === item.id
      && !(c.messages || []).some(message => message.sequence > item.sequence);
  }
  function questionsEnabled(item) {
    return questionsCurrent(item) && !sending && !data.busy
      && !data.conversation.pending_count && !data.conversation.paused;
  }
  function showQuestions(entry, item) {
    const questions = item.questions;
    const storageKey = `contribos:question-draft:${taskId}:${item.id}`;
    const binding = JSON.stringify([item.questionHash, questions]);
    let saved;
    try { saved = JSON.parse(sessionStorage.getItem(storageKey)); } catch { /* Start with no selection. */ }
    if (saved?.binding !== binding) saved = null;
    const answers = questions.map((q, i) => {
      const answer = saved?.answers?.[i];
      return {choice:answer?.choice === "custom" || (Number.isInteger(answer?.choice) && answer.choice >= 0 && answer.choice < (q.options || []).length) ? answer.choice : null,
        text:typeof answer?.text === "string" ? answer.text.slice(0, 8000) : ""};
    });
    let attempt = saved?.attempt;
    function persist() {
      try { sessionStorage.setItem(storageKey, JSON.stringify({binding, answers, attempt})); } catch { /* Choices remain in memory. */ }
    }
    const panel = node("div", "", "conversation-questions");
    panel.append(node("p", "每题选择一项，也可以填写自定义回答。全部答好后一次发送。"));
    const fields = questions.map((question, index) => {
      const field = node("fieldset", "", "conversation-question");
      field.append(node("legend", `${index + 1}. ${friendly(question.prompt)}`));
      const answer = answers[index], radios = [];
      const custom = node("textarea"); custom.rows = 2; custom.maxLength = 8000;
      custom.placeholder = "填写你的回答…";
      custom.setAttribute("aria-label", `${friendly(question.prompt)}：自定义回答`);
      function choose(choice, label) {
        const wrapper = node("label", "", "conversation-question-option");
        const radio = node("input"); radio.type = "radio";
        radio.name = `question-${item.id}-${index}`; radio.value = String(choice);
        radio.checked = answer.choice === choice;
        radio.addEventListener("change", () => {
          answer.choice = choice; persist(); update();
          if (choice === "custom") custom.focus();
        });
        wrapper.append(radio, node("span", friendly(label))); field.append(wrapper); radios.push(radio);
      }
      (question.options || []).forEach((option, i) => choose(i, option));
      choose("custom", "自定义回答");
      custom.value = answer.text;
      custom.addEventListener("input", () => { answer.text = custom.value; persist(); update(); });
      field.append(custom); panel.append(field);
      return {field, radios, custom};
    });
    const progress = node("p", "", "conversation-question-progress"); progress.setAttribute("role", "status");
    function answerText(question, index) {
      const answer = answers[index];
      return answer.choice === "custom" ? answer.text.trim() : (question.options || [])[answer.choice] || "";
    }
    function messageText() {
      return "回答你的问题：\n\n" + questions.map((q, i) => `${i + 1}. ${q.prompt}\n我的回答：${answerText(q, i)}`).join("\n\n");
    }
    const submit = button("发送回答", () => {
      update(); if (submit.disabled) return;
      const text = messageText();
      if (!attempt || attempt.text !== text || typeof attempt.key !== "string") attempt = {text, key:crypto.randomUUID()};
      persist();
      act(async () => {
        await post("/conversation/messages", {text, question_source_id:item.id, question_source_hash:item.questionHash}, attempt.key);
        try { sessionStorage.removeItem(storageKey); } catch { /* The receipt also disables old questions. */ }
      });
    }, true);
    function update() {
      const enabled = questionsEnabled(item);
      const count = questions.filter((q, i) => !!answerText(q, i)).length;
      const tooLong = messageText().length > 8000;
      fields.forEach(({field, radios, custom}, i) => {
        field.disabled = !enabled;
        custom.hidden = answers[i].choice !== "custom";
        radios.forEach(radio => radio.closest("label").classList.toggle("is-selected", radio.checked));
      });
      progress.textContent = tooLong ? "回答合并后超过 8000 字，请缩短自定义回答。" : `已回答 ${count}/${questions.length} 个问题`;
      submit.disabled = !enabled || count !== questions.length || tooLong;
    }
    panel.append(progress, submit); entry.body.append(panel); entry.updateQuestions = update; update();
  }
  function previewValid(p, kind) {
    if (!p || p.consumed || Date.now() >= Date.parse(p.expires_at) || !Number.isFinite(Date.parse(p.expires_at))) return false;
    if (p.publisher_account_hash && p.publisher_account_hash !== data.execution?.publisher_account_hash) return false;
    if (kind === "publication") return !latest("publication_confirmed") && latest("publication_prepared")?.record_hash === p.record_hash;
    if (kind === "cla") return data.cla?.preview?.record_hash === p.record_hash && !p.existing_comment && !data.cla?.status?.existing_comment && !data.cla?.submitted && !data.cla?.unresolved && (data.cla?.status?.status || p.status) !== "passed";
    if (kind === "update") return data.pr_update?.preview?.record_hash === p.record_hash && !data.pr_update?.unresolved && !data.pr_lifecycle?.unresolved;
    return data.pr_lifecycle?.preview?.record_hash === p.record_hash && !data.pr_lifecycle?.unresolved && !data.pr_update?.unresolved;
  }
  function publicationBlocker(p) {
    if (p.consumed || latest("publication_confirmed")) return "发布已确认，请返回对话查看处理结果。";
    if (latest("publication_prepared")?.record_hash !== p.record_hash) return "这份预览已被更新，请返回对话查看最新预览。";
    if (p.publisher_account_hash && p.publisher_account_hash !== data.execution?.publisher_account_hash) return "发布账号已变化，请重新准备发布预览。";
    if (sending) return "正在提交请求，请稍候。";
    if (data.conversation?.pending_count) return "正在处理你的消息，完成后可继续确认。";
    if (data.conversation?.paused) return "任务已暂停，请返回对话继续任务后确认。";
    if (data.busy) return "正在处理任务，完成后可继续确认。";
    if (!Number.isFinite(Date.parse(p.expires_at)) || Date.now() >= Date.parse(p.expires_at)) return "预览已过期。请沿用当前标题和正文更新预览，重新核对后再创建草稿 PR。";
    return "";
  }
  function confirmation(label, valid, handler) {
    const el = button(label, () => act(handler), true); el.dataset.confirmation = "true"; el.dataset.valid = String(valid); return el;
  }
  function resourcePreviewValid(p) {
    const current = data?.native_resources?.preview;
    return !!current && current.record_hash === p.record_hash && current.current && !current.consumed
      && Number.isFinite(Date.parse(p.expires_at)) && Date.now() < Date.parse(p.expires_at);
  }
  async function prepareResources() {
    const p = await post("/native-resources/prepare", {});
    showResourcePreview(p);
  }
  function showResourcePreview(p) {
    openDrawer("授权下载固定测试资源");
    const r = p.resource;
    drawerBody.append(node("p", r.purpose), node("h3", r.name), node("p", `版本：${r.version} · 平台：${r.platform}`));
    drawerBody.append(safeLink("下载来源：GitHub Release", r.url), node("pre", r.url));
    drawerBody.append(node("p", "归档 SHA-256"), node("pre", r.sha256));
    drawerBody.append(node("p", `下载上限 ${r.download_bytes / 1024**2} MiB，展开上限 ${r.expanded_bytes / 1024**2} MiB，每步最多 ${r.timeout_seconds} 秒。`));
    drawerBody.append(node("p", "仅下载这份归档，校验后提取 ffmpeg 和 ffprobe。测试仍断网运行；资源授权后，执行方案还需要单独确认。"));
    drawerBody.append(node("p", `本次预览有效至 ${new Date(p.expires_at).toLocaleString("zh-CN")}。`));
    const state = node("p"); state.dataset.previewStatus = "true";
    state.update = () => { state.textContent = resourcePreviewValid(p) ? "核对后可授权本次下载。" : "本次预览已使用、过期或依据已变化；可以重新生成。"; };
    const authorize = confirmation("授权下载这些资源", resourcePreviewValid(p), async () => {
      await post("/native-resources/confirm", {preview_hash:p.record_hash, nonce:p.nonce, approved:true});
      drawer.close();
    });
    // The preview can be created while this dialog is open; polling rechecks its binding.
    authorize.dataset.valid = "true"; authorize.validate = () => resourcePreviewValid(p);
    drawerBody.append(state, authorize, button("重新生成资源预览", () => act(prepareResources)));
    controls();
  }
  function executionBinding(plan) {
    return JSON.stringify([plan.id, plan.record_hash, data.context?.record_hash,
      data.model_binding_hash, data.execution?.preflight?.report_hash, data.native_resources?.receipt_hash]);
  }
  function canExecutePlan(plan, binding) {
    const current = data.plans?.at(-1);
    return data.state === "planning" && !!data.execution?.actions?.can_execute
      && current?.id === plan.id && current.record_hash === plan.record_hash
      && executionBinding(plan) === binding;
  }
  async function executePlan(plan, binding, modelBinding) {
    if (!canExecutePlan(plan, binding)) throw new Error("方案或执行依据已变化，请重新核对当前方案。");
    await post("/execute", {plan_id:plan.id, plan_hash:plan.record_hash,
      approve_plan:true, start_execution:true, max_repairs:2, model_binding_hash:modelBinding || null, conversation:true});
    if (drawer.open) drawer.close();
  }
  function showPlan(plan) {
    const binding = executionBinding(plan), modelBinding = data.model_binding_hash;
    openDrawer("贡献方案");
    for (const [key,label] of planFields) {
      const value = plan[key]; if (!value?.length) continue;
      drawerBody.append(node("h3", label));
      if (Array.isArray(value)) list(drawerBody, value);
      else { const body = node("div"); renderMarkdown(body, friendly(value)); drawerBody.append(body); }
    }
    if (plan.commands_to_run?.length) {
      drawerBody.append(node("h3", "验证命令"));
      for (const command of plan.commands_to_run) drawerBody.append(node("pre", JSON.stringify(command, null, 2)));
    }
    drawerBody.append(node("p", "确认后先准备隔离环境并运行修改前测试，再实现、断网验证和独立审查。范围内最多自动修复两轮。"));
    const start = confirmation("确认方案并开始", true, () => executePlan(plan, binding, modelBinding));
    start.validate = () => canExecutePlan(plan, binding);
    const state = node("p"); state.setAttribute("role", "status"); state.dataset.previewStatus = "true";
    state.update = () => {
      const current = data.plans?.at(-1);
      state.textContent = current?.id !== plan.id || current?.record_hash !== plan.record_hash
        ? "这是历史方案，请返回对话核对最新方案。"
        : executionBinding(plan) !== binding ? "执行依据已变化，请关闭并重新核对方案。"
        : data.busy ? "当前工作正在处理，完成后可继续确认。"
        : data.conversation?.pending_count ? "还有补充信息正在处理，完成后可继续确认。"
        : data.conversation?.paused ? "任务已停止，请返回对话继续任务后确认。"
        : !data.execution?.actions?.can_execute ? "当前执行条件尚未满足，请返回对话查看原因。"
        : "核对修改范围和测试安排后，可在这里确认并开始。";
    };
    drawerBody.append(state, start, node("p", "需要修改时，返回对话直接告诉我即可。"));
    controls();
  }
  function verificationText() {
    const v = data.execution?.verification;
    return v?.status === "passed" ? `${v.passed}/${v.total} 条验证命令通过；项目测试中的跳过情况请查看详细结果。`
      : v?.status === "environment_blocked" ? "测试环境尚未就绪，当前不能确认测试通过。"
      : v?.status === "failed" ? "验证尚未通过，已有修改保留。" : "尚无完整的测试结果。";
  }
  async function showExecution(id = data.execution?.latest_attempt_id || data.execution?.last_attempt_id) {
    const generation = openDrawer("改动、测试与代码审查");
    drawerBody.append(node("p", "正在读取结果…"));
    try {
      if (!id) { drawerBody.replaceChildren(node("p", "尚无执行结果。")); return; }
      const detail = await requestJSON(`/api/v1/executions/${encodeURIComponent(id)}`);
      if (disposed || generation !== drawerGeneration) return;
      const outcome = data.execution?.attempts?.find(a => a.id === id)?.verification || data.execution?.verification;
      drawerBody.replaceChildren(node("h3", "代码改动"));
      changes = createExecutionChanges(); drawerBody.append(changes.summary, changes.viewer);
      await changes.update({...detail, verification:outcome});
      if (generation !== drawerGeneration) return;
      drawerBody.append(node("h3", "测试结果"));
      if (outcome) {
        drawerBody.append(node("p", `${outcome.passed || 0}/${outcome.total || 0} 条验证命令通过。`));
        for (const failure of outcome.failures || []) drawerBody.append(node("pre", `${failure.command_id || "验证命令"}\n${failure.output || failure.stderr || "未通过"}`));
      }
      drawerBody.append(node("h3", "独立代码审查"));
      for (const review of detail.reviews || []) {
        drawerBody.append(node("p", ({pass:"审查通过",block:"审查未通过",fail:"审查未通过"})[review.verdict] || "审查结果尚未确定"));
        for (const finding of review.findings || []) drawerBody.append(node("p", friendly(finding.message || finding.description || JSON.stringify(finding))));
      }
      if (!detail.reviews?.length) drawerBody.append(node("p", "尚无独立审查结果。"));
      drawerBody.append(node("h3", "完整测试与执行记录"));
      for (const manifest of detail.artifact_manifests || []) {
        for (const artifact of manifest.entries || []) {
          if (["unified-diff", "file-inventory"].includes(artifact.role)) continue;
          const expected = `/api/v1/executions/${encodeURIComponent(id)}/artifacts/${artifact.artifact_id}`;
          if (artifact.content_url !== expected || !/^[a-f0-9]{64}$/.test(artifact.artifact_id)) continue;
          const link = node("a", `${({explore:"代码阅读", implement:"实现", verify:"测试"})[manifest.stage] || "执行"} · ${artifact.role}`);
          link.href = expected; link.target = "_blank"; link.rel = "noopener noreferrer"; drawerBody.append(link, node("br"));
        }
      }
    } catch { if (generation === drawerGeneration) drawerBody.replaceChildren(node("p", "结果暂时无法读取，已有成果保留。"), button("重新读取", () => showExecution(id))); }
  }
  function showFollowup(snapshot) {
    openDrawer("项目检查与维护者反馈");
    drawerBody.append(safeLink("在 GitHub 查看 PR", snapshot.target?.pr_url));
    drawerBody.append(node("p", `上次核对：${new Date(snapshot.observed_at).toLocaleString("zh-CN")}`));
    drawerBody.append(node("p", friendly(snapshot.next_action)));
    drawerBody.append(node("h3", "项目自动检查"));
    const labels={success:"通过",failure:"未通过",action_required:"等待维护者批准",completed:"已结束",in_progress:"进行中",queued:"排队中",pending:"等待中",skipped:"已跳过",cancelled:"已取消",timed_out:"超时",neutral:"无检查结论"};
    for (const check of [...(snapshot.checks || []), ...(snapshot.workflows || []), ...(snapshot.statuses || [])]) {
      const value=check.conclusion || check.state || check.status;
      drawerBody.append(node("p", `${check.name || check.context || "项目检查"}：${labels[value] || "结果尚未确定"}`));
    }
    for (const failure of snapshot.failures || []) {
      drawerBody.append(node("h3", friendly(failure.name)));
      for (const step of failure.steps || []) drawerBody.append(node("p", friendly(step.name)));
    }
    for (const annotation of snapshot.annotations || []) drawerBody.append(node("p", friendly(`${annotation.path}:${annotation.line} ${annotation.message}`)));
    drawerBody.append(node("h3", "审核意见与讨论"));
    for (const comment of [...(snapshot.reviews || []), ...(snapshot.comments || [])]) {
      drawerBody.append(node("p", `${comment.login || "维护者"}：${friendly(comment.body)}`));
      if (comment.url) drawerBody.append(safeLink("查看原文", comment.url));
    }
    if (!snapshot.reviews?.length && !snapshot.comments?.length) drawerBody.append(node("p", "上次核对时暂无可读取的意见。"));
    if (snapshot.warnings?.length) drawerBody.append(node("p", "部分记录尚不能完整读取，不能据此判定所有项目检查通过。"));
  }
  function showHistory() {
    openDrawer("历史记录与诊断");
    drawerBody.append(node("p", "以下保留原始内容和记录，用于追溯当时的结果。旧确认不会再次执行。"));
    for (const plan of data.plans || []) drawerBody.append(button(`查看第 ${plan.version_number} 版方案`, () => showPlan(plan)));
    for (const attempt of data.execution?.attempts || []) drawerBody.append(button(`查看第 ${attempt.number} 次尝试`, () => showExecution(attempt.id)));
    for (const entry of data.events || []) {
      drawerBody.append(node("h3", `${entry.sequence || ""} · ${entry.kind}`));
      drawerBody.append(node("pre", JSON.stringify(entry.payload, null, 2)));
    }
  }
  function showPreview(kind, p, historical = false) {
    openDrawer(historical ? "历史发布预览" : ({publication:"创建草稿 PR", update:"更新 PR 文案", ready:"提交正式审核", cla:"签署贡献者协议"})[kind]);
    const target = p.target || {};
    drawerBody.append(node("p", `发布账号：${p.login || p.fork?.split("/")[0] || "当前发布账号"}`));
    drawerBody.append(target.pr_url ? safeLink("查看目标 PR", target.pr_url) : node("p", `目标仓库：${p.repository} · ${p.base_branch}`));
    if (kind === "publication" && !historical) {
      drawerBody.append(node("p", "将使用上述账号上传这些改动，并创建一个草稿 PR。发布范围仅限本次预览；后续提交正式审核需要另外确认。"));
      drawerBody.append(button("查看本次代码、测试与审查", () => showExecution()));
    }
    if (kind === "update") {
      drawerBody.append(node("h3", "修改前"), node("pre", `${p.before.title}\n\n${p.before.body}`), node("h3", "修改后"));
      drawerBody.append(node("p", "本次仅更新下方标题与正文。"));
    }
    if (kind === "ready") {
      drawerBody.append(node("p", "将把这个草稿 PR 提交正式审核并通知维护者；项目检查是否通过，以项目显示的结果为准。"));
      drawerBody.append(node("h3", p.before.title), node("pre", p.before.body));
    } else if (kind === "cla") {
      drawerBody.append(node("p", "请阅读本次固定版本的完整协议。签署属于你的法律决定，需要你亲自同意。"));
      drawerBody.append(safeLink("协议来源", p.agreement_url), node("pre", p.agreement_text, "conversation-agreement"), node("h3", "将提交的签署声明"), node("pre", p.comment));
    } else {
      // Show exact public text, including the marker, without rewriting evidence.
      drawerBody.append(node("h3", p.title), node("pre", p.body)); list(drawerBody, p.notes);
    }
    for (const source of p.requirements?.sources || []) drawerBody.append(node("p", `仓库规范：${source.path}`));
    if (historical) {
      drawerBody.append(node("p", "这是创建草稿 PR 时的历史预览，仅供查看。当前操作请使用对话中的最新预览入口。"));
      return;
    }
    drawerBody.append(node("p", `本次预览有效至 ${new Date(p.expires_at).toLocaleString("zh-CN")}。`));
    let consent;
    if (kind === "cla") {
      const label = node("label", "", "conversation-legal-consent"); consent = node("input"); consent.type = "checkbox";
      consent.addEventListener("change", controls);
      label.append(consent, document.createTextNode("我已阅读并同意上述协议，并有权以所展示账号签署。")); drawerBody.append(label);
    }
    const action = confirmation(({publication:"创建草稿 PR", update:"确认更新文案", ready:"提交正式审核", cla:"同意并确认签署"})[kind], previewValid(p, kind), async () => {
      if (!previewValid(p, kind)) throw new Error("这次预览已经变化或过期，请在对话中重新准备。");
      if (consent && !consent.checked) { consent.focus(); throw new Error("请亲自阅读并勾选同意当前协议。"); }
      const common = {preview_hash:p.record_hash, nonce:p.nonce};
      if (kind === "publication") await post("/publication/confirm", {intent_hash:p.record_hash, nonce:p.nonce});
      else if (kind === "cla") await post("/cla/confirm", {...common, agreement_hash:p.agreement_hash, acknowledgement:p.acknowledgement, read_and_agree:true});
      else await post(kind === "ready" ? "/pr-ready/confirm" : "/pr-update/confirm", {...common, text:p.acknowledgement, approved:true});
      drawer.close();
    });
    action.validate = () => previewValid(p, kind) && (!consent || consent.checked);
    drawerBody.append(action);
    if (kind === "publication") {
      const reason = node("p"); reason.setAttribute("role", "status"); reason.dataset.previewStatus = "true";
      const renew = button("沿用文案更新预览", () => act(async () => {
        await post("/publication/renew", {preview_hash:p.record_hash});
        drawer.close();
      }));
      reason.update = () => {
        reason.textContent = publicationBlocker(p);
        renew.hidden = previewValid(p, kind) || p.consumed || !!latest("publication_confirmed") || latest("publication_prepared")?.record_hash !== p.record_hash || !!(p.publisher_account_hash && p.publisher_account_hash !== data.execution?.publisher_account_hash);
        renew.disabled = sending || !!data.busy || !!data.conversation?.pending_count || !!data.conversation?.paused;
      };
      drawerBody.append(reason, renew);
    } else if (!previewValid(p, kind)) drawerBody.append(node("p", "这条记录已处理、已变化或已过期。需要继续时，在对话中说明即可。"));
    controls();
  }
  function addEntry(item) {
    let entry = entries.get(item.id);
    if (!entry) {
      const el = node("article", "", `conversation-agent-message ${item.user ? "is-user" : "is-assistant"}`);
      const role = node("span", item.user ? "你" : "贡献助手", "conversation-message-role");
      const body = node("div", "", "conversation-message-body"); const actions = node("div", "", "conversation-message-actions");
      const receipt = node("small", "", "conversation-message-receipt"); receipt.setAttribute("role", "status");
      el.append(role, body, actions, receipt); entry = {el, body, actions, receipt}; entries.set(item.id, entry);
    }
    const signature = JSON.stringify([item.text, item.questions, item.questionHash, questionsCurrent(item), item.plan?.record_hash,
      item.plan && executionBinding(item.plan), item.action, item.valid, item.bindingHash, item.preview?.record_hash, item.receipt, item.references, latest("publication_completed")?.id, latest("publication_prepared")?.record_hash]);
    if (entry.signature !== signature) {
      entry.signature = signature; entry.body.replaceChildren(); entry.actions.replaceChildren();
      entry.updateQuestions = null;
      renderMarkdown(entry.body, item.user ? item.text : friendly(item.text));
      for (const reference of item.references || []) {
        if (!["publication_preview", "ready_preview"].includes(reference.name)) continue;
        const kind = reference.name === "ready_preview" ? "ready" : "publication";
        const source = data.events.find(e => e.id === reference.source_id && e.record_hash === reference.source_hash && e.kind === (kind === "ready" ? "pr_ready_prepared" : "publication_prepared"));
        if (!source) continue;
        const historical = kind === "publication" && (!!latest("publication_completed") || !!latest("publication_confirmed") || latest("publication_prepared")?.record_hash !== source.record_hash);
        const details = node("details"), summary = node("summary", kind === "ready" ? "审核预览 · 完整标题与正文" : historical ? "历史发布预览 · 完整标题与正文" : "发布预览 · 完整标题与正文");
        details.open = true; details.append(summary, node("h4", kind === "ready" ? source.payload.before?.title : source.payload.title));
        const content = node("div"); renderMarkdown(content, kind === "ready" ? source.payload.before?.body : source.payload.body); details.append(content); entry.body.append(details);
        const p = kind === "ready" ? data.pr_lifecycle?.preview : {...source.payload, record_hash:source.record_hash};
        if (p?.record_hash === source.record_hash) entry.actions.append(button(kind === "ready" ? "查看审核预览" : historical ? "查看历史发布预览" : "查看完整预览", () => showPreview(kind, p, historical)));
      }
      if (item.questions?.length && questionsCurrent(item)) showQuestions(entry, item);
      else for (const question of item.questions || []) {
        entry.body.append(node("p", friendly(question.prompt)));
        list(entry.body, question.options || []);
      }
      if (item.plan) {
        const plan = item.plan; list(entry.body, plan.files_likely_to_change?.slice(0, 6));
        entry.actions.append(button(item.action === "review_plan" ? "核对方案并继续" : "查看方案", () => showPlan(plan), item.action === "review_plan"));
        if (item.action === "execute") {
          const binding = executionBinding(plan), modelBinding = data.model_binding_hash;
          entry.body.append(node("p", "确认后，我会在隔离环境实现、测试和独立审查；范围内最多自动修复两轮。"));
          const start = confirmation("按此方案开始", item.valid, () => executePlan(plan, binding, modelBinding));
          start.validate = () => canExecutePlan(plan, binding);
          entry.actions.append(start);
        }
      }
      if (item.action === "accept") {
        const binding = data.execution.acceptance.binding_hash;
        entry.actions.append(button("查看改动与测试", () => showExecution()));
        entry.actions.append(confirmation("确认结果", item.valid, () => post("/acceptance", {
          binding_hash:binding, text:"我已查看并确认本次代码、测试和独立审查结果。", prepare_pr:true,
        })));
      }
      if (item.action === "prepare_publication") {
        const requestKey = crypto.randomUUID();
        entry.actions.append(confirmation("继续准备 PR 文案和预览", item.valid, () =>
          post("/publication/continue", {binding_hash:item.bindingHash}, requestKey)));
      }
      if (item.preview) entry.actions.append(button(item.action === "ready" ? "查看审核预览" : "查看完整预览", () => showPreview(item.action, item.preview)));
      if (item.action === "followup_result") entry.actions.append(button("查看项目检查与反馈", () => showFollowup(item.snapshot)));
      if (item.action === "settings") { const link = node("a", "前往设置"); link.href = "#/settings"; entry.actions.append(link); }
      if (item.action === "native_resources") {
        const resource = data.native_resources;
        if (!resource.ready && !["queued", "leased", "running"].includes(resource.status)) {
          entry.actions.append(confirmation(resource.preview?.current && !resource.preview.consumed ? "查看资源下载授权" : "准备资源下载授权", true,
            resource.preview?.current && !resource.preview.consumed ? async () => showResourcePreview(resource.preview) : prepareResources));
        }
      }
      if (item.action === "start_planning") {
        const start = button("读取仓库并准备方案", () => act(() => post("/conversation/messages", {text:"读取仓库并准备方案"})), true);
        start.dataset.startPlanning = "true"; entry.actions.append(start);
      }
      entry.receipt.textContent = item.receipt ? receiptLabels[item.receipt] || "已收到" : "";
    }
    return entry.el;
  }
  function render() {
    const bottom = atBottom(), before = entries.size;
    const c = data.conversation || {}, state = c.user_state || {};
    status.textContent = state.label || "等待下一步";
    if (state.observed_at) status.append(node("small", `上次核对：${new Date(state.observed_at).toLocaleString("zh-CN")}`));
    const items = [], newStart = c.messages?.[0]?.sequence ?? Infinity;
    const resources = data.native_resources;
    if (resources?.available && (data.state === "planning" || ["queued","leased","running"].includes(resources.status))) {
      const progress = ({queued:"资源下载已授权，等待准备。", leased:"资源下载任务已领取。", running:"正在下载并校验固定测试资源。",
        ready:"固定测试资源已准备好。请核对当前方案，再单独确认执行。"})[resources.status];
      items.push({id:"native-resources:" + JSON.stringify(resources), sequence:(data.events?.at(-1)?.sequence || 0) + 0.15,
        text:progress || resources.error || "测试需要固定版本的 FFmpeg / ffprobe。请查看下载来源、版本、校验信息与大小限制，再决定是否授权下载。",
        action:"native_resources"});
    }
    if (newStart === Infinity && !latest("pr_followup_observed")) items.push({id:"welcome", sequence:0, text:state.next_step || "直接告诉我你希望完成什么，我会接着处理。",
      action:data.state === "planning" && !data.plans?.length && !data.jobs?.length && !state.busy && !c.paused && data.planner_available ? "start_planning" : undefined});
    for (const message of c.messages || []) items.push({...message, user:true, receipt:message.receipt?.status});
    for (const result of c.results || []) items.push({...result, action:result.source_kind === "awaiting_acceptance" && data.execution?.actions?.can_accept ? "accept" : undefined, valid:!!data.execution?.actions?.can_accept});
    if (["prepare_publication","publisher_setup"].includes(c.pending_action?.kind)) items.push({
      id:"publication-next:" + c.pending_action.binding_hash,
      sequence:(data.events?.at(-1)?.sequence || 0) + 0.3,
      text:c.pending_action.kind === "publisher_setup" ? "当前结果已确认。请先在设置中连接发布账号，再继续准备发布预览。"
        : "当前结果已确认。可以继续准备 PR 文案和完整发布预览，创建草稿 PR 仍需单独确认。",
      action:c.pending_action.kind === "publisher_setup" ? "settings" : "prepare_publication",
      bindingHash:c.pending_action.binding_hash, valid:true});
    const observed = latest("pr_followup_observed");
    if (observed) {
      const existing = items.find(i => i.source_id === observed.id);
      if (existing) Object.assign(existing, {action:"followup_result", snapshot:observed.payload});
      else items.push({id:"followup:" + observed.id, sequence:observed.sequence,
        text:state.label + "。\n\n" + state.next_step, action:"followup_result", snapshot:observed.payload});
    }
    const plan = data.plans?.at(-1), planReply = data.events?.filter(e => e.kind === "assistant_message" && e.payload.plan_id === plan?.id).at(-1);
    const lastReply = latest("assistant_message");
    for (const event of data.events || []) {
      if (event.kind === "assistant_message" && (event.sequence >= newStart || (newStart === Infinity && event.id === lastReply?.id && (!plan || event.payload.questions?.length)))) items.push({id:event.id, sequence:event.sequence,
        text:event.payload.reply || (event.payload.questions?.length ? "需要先确认下面的问题。" : "正在继续阅读代码。"), questions:event.payload.questions, questionHash:event.record_hash,
        plan:event.payload.plan_id ? data.plans.find(p => p.id === event.payload.plan_id) : undefined});
    }
    if (plan && (data.state === "planning" || !latest("publication_completed"))) {
      const id = planReply?.sequence >= newStart ? planReply.id : "plan:" + plan.id;
      const current = {id, sequence:planReply?.sequence || 0.1, text:"方案已准备好。" + plan.goal + "\n\n" + (plan.tests_to_add_or_run?.slice(0, 3).join("\n") || "具体测试安排见方案。"), plan,
        action:data.state === "planning" && data.execution?.actions?.can_execute ? "execute" : undefined, valid:!!data.execution?.actions?.can_execute};
      const existing = items.find(i => i.id === id); existing ? Object.assign(existing, current) : items.push(current);
    }
    if (data.execution?.actions?.can_accept && !items.some(i => i.action === "accept")) items.push({id:"result:" + data.execution.acceptance.binding_hash,
      sequence:latest("awaiting_acceptance")?.sequence || 0.2, text:"修改结果已准备好。" + verificationText() + "\n\n确认结果后，我会准备英文 PR 文案与完整发布预览。", action:"accept", valid:true});
    const published = latest("publication_completed"), preview = latest("publication_prepared");
    const terminalPR = ["merged","closed"].includes(data.pr_lifecycle?.snapshot?.stage) || ["merged","rewarded"].includes(data.state);
    const candidates = (terminalPR ? [] : [
      {kind:"publication", event:preview, p:preview && {...preview.payload, record_hash:preview.record_hash}, eligible:!published && !latest("publication_confirmed")},
      {kind:"update", event:latest("pr_update_prepared"), p:data.pr_update?.preview, eligible:!data.pr_update?.preview?.consumed},
      {kind:"ready", event:latest("pr_ready_prepared"), p:data.pr_lifecycle?.preview, eligible:!data.pr_lifecycle?.preview?.consumed},
      {kind:"cla", event:latest("cla_prepared"), p:data.cla?.preview, eligible:!data.cla?.preview?.consumed && !data.cla?.submitted && data.cla?.status?.status !== "passed"},
    ]).filter(x => x.p && x.eligible).sort((a,b) => (b.event?.sequence || 0) - (a.event?.sequence || 0)).slice(0, 1);
    for (const candidate of candidates) items.push({id:`preview:${candidate.p.record_hash}`, sequence:candidate.event?.sequence || (data.events?.at(-1)?.sequence || 0) + 0.1,
      text:({publication:"发布预览已准备好。请查看账号、目标仓库、完整标题与正文，再决定是否创建草稿 PR。", update:"文案修订已准备好。请查看修改前后内容，再确认更新。", ready:"提交正式审核的预览已准备好，请核对后单独确认。", cla:"贡献者协议已读取。请阅读完整协议，是否同意由你决定。"})[candidate.kind], action:candidate.kind, preview:candidate.p});
    if (!data.planner_available && !published && !state.busy) items.push({id:"setup", sequence:0.05, text:"讨论或执行服务尚未准备好。消息可以保存；请在设置中检查模型与服务配置。", action:"settings"});
    const blockers = data.execution?.readiness?.blockers || [];
    if (blockers.length && !state.busy && !published) items.push({id:"readiness:" + JSON.stringify(blockers), sequence:(data.events?.at(-1)?.sequence || 0) + 0.2, text:blockers.map(b => b.message).join("\n")});
    if (plan && data.state === "planning" && c.pending_action?.kind === "execute"
        && c.pending_action.source_id === plan.id && data.execution?.actions?.can_execute && !published) {
      items.push({id:"next-plan:" + plan.id, sequence:items.reduce((last, item) => Math.max(last, item.sequence || 0), 0) + 0.5,
        text:resources?.ready ? "测试资源已就绪。点击下方核对当前方案，在详情中确认后即可开始修改前测试。"
          : "当前方案可以继续。点击下方核对修改范围和测试安排，在详情中确认后开始。",
        plan, action:"review_plan"});
    }
    items.sort((a,b) => a.sequence - b.sequence);
    const currentIds = new Set(items.map(i => i.id));
    for (const [id,entry] of entries) if (!currentIds.has(id)) { entry.el.remove(); entries.delete(id); }
    let cursor = messages.firstElementChild;
    for (const item of items) { const el = addEntry(item); if (el === cursor) cursor = cursor.nextElementSibling; else messages.insertBefore(el, cursor); }
    if (bottom) messages.scrollTop = messages.scrollHeight; else if (entries.size > before) fresh.hidden = false;
    controls();
  }
  async function load(force = false) {
    clearTimeout(timer); if (disposed) return;
    if (loading) { if (force) { await loading; return load(); } return loading; }
    loading = requestJSON(endpoint);
    try { const next = await loading; if (!disposed) { data = next; render(); } }
    catch { if (!disposed) error.textContent = "任务暂时无法读取，消息和已有成果会保留。"; }
    finally { loading = null; if (!disposed) timer = setTimeout(() => load(), 2000); }
  }
  load();
  return {dispose() { disposed = true; clearTimeout(timer); viewport?.removeEventListener("resize", fitKeyboard); persistDraft(); changes?.dispose(); if (drawer.open) drawer.close(); }};
}
