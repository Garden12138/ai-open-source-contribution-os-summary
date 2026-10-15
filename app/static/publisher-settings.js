import {requestJSON} from "./api.js?v=studio-v1";
import {el, action} from "./studio.js?v=studio-v1";
import {sealCredential} from "./model-settings.js?v=studio-v3";

export function mountPublisherSettings(root, options) {
  let data = null, busy = false, timer = null, pending = null, disconnecting = false;
  const shell = el("section", "", "studio-model-settings");
  shell.id = "github-publisher-settings";
  shell.append(el("h2", "GitHub 发布"), el("p", "连接用于创建 Draft PR 的 GitHub 账号。连接和检测只读取账号信息，每次发布仍需确认精确预览。"));
  const status = el("p", "正在检查发布服务…", "settings-status"); status.setAttribute("role", "status");
  const content = el("div", "", "settings-section"); shell.append(status, content); root.replaceChildren(shell);
  async function mutate(path, body, key = crypto.randomUUID()) {
    await options.ensureAccess();
    return requestJSON("/api/v1/publisher-settings/" + path, {method:"POST",
      headers:options.headers({"Content-Type":"application/json", "Idempotency-Key":key}), body:JSON.stringify(body)});
  }
  async function load() {
    clearTimeout(timer);
    try { data = await requestJSON("/api/v1/publisher-settings"); render(); }
    catch (error) { status.textContent = error.message; content.replaceChildren(action("重新检查服务", load)); }
    if (data?.state === "pending" && ["queued","leased","running"].includes(data.job_state)) timer = setTimeout(load, 1500);
  }
  async function run(fn) {
    if (busy) return;
    busy = true; for (const button of content.querySelectorAll("button")) button.disabled = true;
    try { await fn(); disconnecting = false; await load(); await options.onSaved?.(); }
    catch (error) { status.textContent = error.message; }
    finally { busy = false; for (const button of content.querySelectorAll("button")) button.disabled = false; }
  }
  function render() {
    content.replaceChildren();
    const active = data.state === "pending" && ["queued","leased","running"].includes(data.job_state);
    const labels = {unavailable:"发布服务未连接，请检查本地服务后重试。", disconnected:"尚未连接 GitHub 发布账号。",
      pending:"正在后台检测 GitHub 账号及公开仓库权限…", connected:`已连接 @${data.login} · 可准备 Draft PR 发布预览。`,
      verification_failed:data.error || "账号检测未完成，请重新检测或更换 Token。"};
    status.textContent = labels[data.state];
    if (!data.available) { content.append(action("重新检查服务", load)); return; }
    if (data.connected) {
      const account = el("a", `在 GitHub 查看 @${data.login}`); account.href = `https://github.com/${encodeURIComponent(data.login)}`;
      account.target = "_blank"; account.rel = "noopener noreferrer";
      content.append(account, el("p", `已检测权限：${data.scopes.join("、")}。具体仓库与分支在发布预览时继续核对。`));
      if (data.checked_at) content.append(el("p", `最近检测：${new Date(data.checked_at).toLocaleString()}`, "muted"));
    }
    if (active) {
      content.append(action("取消检测", () => run(async () => {
        await options.ensureAccess();
        await requestJSON(`/api/v1/jobs/${data.job_id}/cancel`, {method:"POST", headers:options.headers()});
      })));
      return;
    }
    const form = el("form"), label = el("label", data.connected ? "更换发布 Token" : "GitHub Classic Token", "settings-wide");
    const secret = el("input"); secret.type = "password"; secret.autocomplete = "new-password"; secret.required = !pending;
    secret.maxLength = 260; secret.placeholder = "ghp_…"; secret.setAttribute("aria-label", "GitHub 发布 Token");
    label.append(secret); form.append(label);
    const help = el("p", "公开仓库贡献使用 Classic Token 的 public_repo 权限。Token 加密提交，仅由独立发布服务保存，不会回显。", "muted settings-wide");
    const tokenLink = el("a", "在 GitHub 创建发布 Token"); tokenLink.href = "https://github.com/settings/tokens/new?scopes=public_repo&description=ContribOS%20Draft%20PR";
    tokenLink.target = "_blank"; tokenLink.rel = "noopener noreferrer";
    form.append(help, tokenLink);
    const save = action(pending ? "重试保存连接" : data.connected ? "连接新账号" : "连接 GitHub 账号", () => {}); save.type = "submit";
    form.append(save); content.append(form);
    secret.addEventListener("input", () => { pending = null; secret.required = true; });
    form.addEventListener("submit", event => { event.preventDefault(); run(async () => {
      if (!pending) {
        const token = secret.value.trim();
        if (!/^ghp_[A-Za-z0-9]{20,255}$/.test(token)) throw new Error("请填写带 public_repo 权限的 Classic Token（ghp_ 开头）。");
        const connection_id = crypto.randomUUID();
        const grant = await mutate("credential-grants", {connection_id});
        const envelope = await sealCredential(token, grant); secret.value = "";
        pending = {connection_id, envelope, expected_hash:data.account_hash, key:crypto.randomUUID()};
        secret.required = false;
      }
      const {key, ...body} = pending;
      await mutate("credentials", body, key); pending = null;
    }); });
    const actions = el("div", "", "settings-actions");
    if (data.job_id || data.connected) actions.append(action("重新检测账号", () => run(() => mutate("test", {expected_hash:data.account_hash}))));
    if (data.account_hash && data.state !== "disconnected") actions.append(action("断开连接", () => { disconnecting = true; render(); }));
    actions.append(action("刷新状态", load)); content.append(actions);
    if (disconnecting) {
      const confirm = el("div"); confirm.setAttribute("role", "group"); confirm.setAttribute("aria-label", "断开 GitHub 发布连接确认");
      confirm.append(el("p", "确认从本机移除发布凭证？已有代码与验收结果保留。此操作不会撤销 GitHub 上的 Token。"),
        action("确认断开", () => run(() => mutate("disconnect", {expected_hash:data.account_hash}))),
        action("取消", () => {disconnecting = false; render();}));
      content.append(confirm);
    }
  }
  load();
  return {refresh:load, dispose(){clearTimeout(timer);}};
}
