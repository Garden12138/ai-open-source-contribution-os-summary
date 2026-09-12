import { requestJSON } from "./api.js?v=studio-v1";

export function el(tag, text = "", className = "") {
  const node = document.createElement(tag); node.textContent = text; node.className = className; return node;
}
export function action(text, callback, className = "analysis-action") {
  const node = el("button", text, className); node.type = "button"; node.addEventListener("click", callback); return node;
}

export function initStudioChrome() {
  const themeButton = document.querySelector("#theme-toggle");
  let theme = "light";
  try { theme = localStorage.getItem("studio-theme") || "light"; } catch {}
  const applyTheme = () => {
    document.documentElement.dataset.theme = theme;
    themeButton.textContent = theme === "dark" ? "切换浅色" : "切换深色";
    themeButton.setAttribute("aria-label", themeButton.textContent + "模式");
    document.querySelector('meta[name="theme-color"]').content = theme === "dark" ? "#212121" : "#ffffff";
  };
  themeButton.addEventListener("click", () => {
    theme = theme === "dark" ? "light" : "dark";
    try { localStorage.setItem("studio-theme", theme); } catch {}
    applyTheme();
  });
  applyTheme();
  const toggle = document.querySelector("#sidebar-toggle");
  toggle.addEventListener("click", () => {
    const opened = document.body.classList.toggle("sidebar-open");
    toggle.setAttribute("aria-expanded", String(opened));
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") { document.body.classList.remove("sidebar-open"); toggle.setAttribute("aria-expanded", "false"); }
  });
  document.querySelector(".site-header").addEventListener("click", event => {
    if (event.target.closest("a")) { document.body.classList.remove("sidebar-open"); toggle.setAttribute("aria-expanded", "false"); }
  });
}

export function compactOpportunity(pick, {open, save, dismiss}) {
  const opportunity = pick.opportunity || {};
  const product = pick.product_recommendation || {};
  const repo = opportunity.repository || {};
  const card = el("article", "", "opportunity-card studio-card");
  card.dataset.reason = pick.selection_reason || "best_available";
  card.dataset.opportunityId = opportunity.id;
  const main = el("div");
  const meta = el("div", "", "studio-card-meta");
  meta.append(el("span", repo.full_name || "开源项目"), el("span", `#${opportunity.issue_number || "—"}`));
  const title = action(opportunity.title || "查看开源机会", open, "studio-card-title");
  const summary = el("p", product.summary || opportunity.body || repo.description || "点击查看 Issue、分析结论与贡献建议。", "studio-card-summary");
  const bottom = el("div", "", "studio-card-bottom");
  bottom.append(el("span", repo.language || "开源"), el("span", product.analysis_status === "analyzed" ? "已完成 AI 分析" : "待 AI 分析"));
  if (opportunity.has_bounty) bottom.append(el("span", opportunity.bounty_amount_usd ? `$${opportunity.bounty_amount_usd} 赏金` : "含赏金"));
  main.append(meta, title, summary, bottom);
  const score = Number(pick.score_snapshot ?? opportunity.score_total);
  const aside = el("div", Number.isFinite(score) ? score.toFixed(0) : "—", "studio-card-score");
  aside.append(el("small", product.analysis_status === "analyzed" ? "推荐分" : "匹配分"));
  const controls = el("div", "", "product-actions");
  controls.append(action(product.disposition_state === "shortlisted" ? "已加入候选" : "加入候选", save, "analysis-action is-secondary"),
    action(product.disposition_state === "dismissed" ? "恢复机会" : "忽略", dismiss, "analysis-action is-secondary"));
  card.append(main, aside, controls);
  card.addEventListener("click", event => {
    if (!event.target.closest("button,a,input,label,select")) open();
  });
  return card;
}

export function renderSidebarTasks(tasks) {
  const root = document.querySelector("#sidebar-tasks"); root.replaceChildren();
  if (!tasks.length) root.append(el("p", "从一个开源机会开始贡献。", "muted"));
  for (const task of tasks.slice(-30).reverse()) {
    const link = el("a"); link.href = `#/tasks/${encodeURIComponent(task.id)}`;
    link.append(el("strong", task.opportunity_title || "贡献任务"), el("small", `${task.repository_full_name || ""} · ${task.friendly_state || task.current_state || "规划中"}`));
    link.title = task.opportunity_title || "贡献任务";
    if (location.hash === link.getAttribute("href")) link.setAttribute("aria-current", "page");
    root.append(link);
  }
}

export function renderMarkdown(root, text) {
  root.replaceChildren();
  let code = null, list = null;
  function inline(target, value) {
    const regex = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\(https:\/\/[^\s)]+\))/g;
    let cursor = 0;
    for (const match of value.matchAll(regex)) {
      target.append(document.createTextNode(value.slice(cursor, match.index)));
      const token = match[0];
      if (token.startsWith("`")) target.append(el("code", token.slice(1, -1)));
      else if (token.startsWith("**")) target.append(el("strong", token.slice(2, -2)));
      else {
        const [, label, url] = token.match(/^\[([^\]]+)\]\((.+)\)$/);
        const link = el("a", label); link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer"; target.append(link);
      }
      cursor = match.index + token.length;
    }
    target.append(document.createTextNode(value.slice(cursor)));
  }
  for (const line of String(text || "").split("\n")) {
    if (line.startsWith("```")) {
      if (code) { code = null; } else { const pre = el("pre"); code = el("code"); pre.append(code); root.append(pre); } list = null; continue;
    }
    if (code) { code.textContent += line + "\n"; continue; }
    if (!line.trim()) { list = null; continue; }
    const item = line.match(/^\s*(?:[-*]|\d+\.)\s+(.+)$/);
    if (item) { if (!list) { list = el("ul"); root.append(list); } const li = el("li"); inline(li, item[1]); list.append(li); continue; }
    list = null;
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    const node = el(heading ? `h${Math.min(heading[1].length + 2, 6)}` : "p"); inline(node, heading ? heading[2] : line); root.append(node);
  }
}
