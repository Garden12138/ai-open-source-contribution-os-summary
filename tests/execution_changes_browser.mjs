// Runs only against the disposable studio_browser_server. No model or sandbox calls.
import {createRequire} from "node:module";
import assert from "node:assert/strict";
import {diff, inventory, executionDetail} from "./execution_changes_fixtures.mjs";
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page = await browser.newPage({viewport:{width:1440,height:1000}});
const errors = [], mutations = [];
page.on("pageerror", error => errors.push(error.message));
page.on("request", req => { if (!["GET","HEAD","OPTIONS"].includes(req.method())) mutations.push(req.url()); });
page.on("dialog", async dialog => { errors.push(dialog.message()); await dialog.dismiss(); });
let detail = executionDetail(), patch = diff, files = inventory, fail = false, detailFail = false, reads = 0;
try {
  await page.goto("http://127.0.0.1:8765");
  await page.locator("#sidebar-tasks a").first().click();
  const taskId = (await page.evaluate(() => location.hash)).split("/").at(-1);
  const taskUrl = `http://127.0.0.1:8765/api/v1/tasks/${taskId}`;
  const task = await (await page.request.get(taskUrl)).json();
  await page.route(taskUrl, route => route.fulfill({json:{...task,latest_execution_attempt_id:detail.id}}));
  await page.route("**/api/v1/executions/*", route => detailFail
    ? route.fulfill({status:409,json:{detail:"Artifact integrity check failed"}}) : route.fulfill({json:detail}));
  await page.route("**/api/v1/executions/*/artifacts/*", route => {
    reads++;
    if (fail) return route.fulfill({status:409,json:{detail:"Artifact integrity check failed"}});
    return route.request().url().endsWith("a".repeat(64))
      ? route.fulfill({contentType:"text/x-diff",body:patch}) : route.fulfill({json:files});
  });
  await page.reload();
  const summary = page.locator(".changes-summary");
  await summary.getByText("已修改 5 个文件",{exact:false}).waitFor();
  detailFail = true;
  await summary.getByText("执行记录读取失败",{exact:false}).waitFor();
  assert.equal(await summary.getByText("已修改 5 个文件",{exact:false}).count(), 0);
  detailFail = false;
  await summary.getByText("已修改 5 个文件",{exact:false}).waitFor();
  assert.notEqual(await page.locator(".ai-workbench-error").textContent(), "Artifact integrity check failed");
  assert.equal(await summary.locator("img").count(), 0);
  await summary.getByRole("button",{name:"app/中文 file.py",exact:false}).click();
  const file = page.locator('.changes-file[data-path="app/中文 file.py"]');
  await file.locator(".changes-add").first().waitFor();
  assert.deepEqual(await file.locator(".changes-add .changes-line-number:nth-child(2)").allTextContents(), ["3","4","21"]);
  assert.equal(await file.locator("img").count(), 0);
  assert.ok((await file.textContent()).includes("<img src=x onerror=alert(1)>"));
  await file.evaluate(el => { el.dataset.keep = "preserved"; el.querySelector(".changes-code-scroll").scrollLeft = 20; });
  const priorReads = reads;
  detail = {...detail,stage_runs:[{stage:"verify",job:{state:"failed"}}]};
  await page.locator(".changes-verification").getByText("测试验证：失败",{exact:false}).waitFor();
  assert.equal(await file.getAttribute("open"), "");
  assert.equal(await file.getAttribute("data-keep"), "preserved");
  assert.equal(reads, priorReads, "Status polling reuses immutable artifacts");
  assert.equal(await summary.count(), 1);
  await page.getByRole("button",{name:"返回对话",exact:true}).click();
  assert.equal(await summary.getByRole("button",{name:"app/中文 file.py",exact:false}).evaluate(el=>el===document.activeElement), true);
  for (const width of [1440,390]) {
    await page.setViewportSize({width,height:900});
    for (const theme of ["light","dark"]) {
      await page.evaluate(theme => document.documentElement.dataset.theme=theme, theme);
      await summary.getByRole("button",{name:"app/中文 file.py",exact:false}).click();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth), true);
      await page.screenshot({path:`/tmp/contribos-changes-${width}-${theme}.png`});
      await page.getByRole("button",{name:"返回对话",exact:true}).click();
    }
  }
  // A new execution must never show the previous execution's modification summary.
  fail = true; detail = executionDetail("execution-retry");
  await summary.getByText("代码变更读取失败",{exact:false}).waitFor();
  assert.equal(await summary.getByText("已修改 5 个文件",{exact:false}).count(), 0);
  fail = false;
  await summary.getByRole("button",{name:"重试读取变更"}).click();
  await summary.getByText("已修改 5 个文件",{exact:false}).waitFor();
  // Empty and pending are distinct from failures.
  files = {changed_paths:[],baseline_inventory:[],result_inventory:[]}; patch = "";
  detail = executionDetail("execution-empty");
  await summary.getByText("本次执行没有代码修改",{exact:true}).waitFor();
  detail = {...executionDetail("execution-pending"),artifact_manifests:[]};
  await summary.waitFor({state:"hidden"});
  // Large patches render a bounded first page, then expand without losing source.
  const path = "large.ts";
  files = {changed_paths:[path],baseline_inventory:[],result_inventory:[{path,size:2000,sha256:"new",executable:false}]};
  patch = `diff --git a/${path} b/${path}\nnew file mode 100644\n--- /dev/null\n+++ b/${path}\n@@ -0,0 +1,601 @@\n` + Array.from({length:601},(_,i)=>`+line ${i+1}\n`).join("");
  detail = executionDetail("execution-large");
  await summary.getByText("已修改 1 个文件 · +601 / −0",{exact:true}).waitFor();
  await summary.getByRole("button",{name:"large.ts",exact:false}).click();
  const large = page.locator('.changes-file[data-path="large.ts"]');
  await large.locator(".changes-row").first().waitFor();
  assert.equal(await large.locator(".changes-row").count(), 300);
  await large.getByRole("button",{name:"加载更多差异行"}).click();
  assert.equal(await large.locator(".changes-row").count(), 600);
  await large.getByRole("button",{name:"加载更多差异行"}).click();
  assert.equal(await large.locator(".changes-row").count(), 602);
  assert.equal(await large.locator(".changes-add .changes-line-number:nth-child(2)").last().textContent(), "601");
  // Invalid patch text keeps the raw fallback, never a zero-line success.
  await page.getByRole("button",{name:"返回对话",exact:true}).click();
  patch = "invalid patch"; detail = executionDetail("execution-malformed");
  await summary.getByText("部分文本行数不可用",{exact:false}).waitFor();
  await summary.getByRole("button",{name:"large.ts",exact:false}).click();
  await page.getByText("无法解析此差异；请查看原始 diff。",{exact:true}).waitFor();
  await page.locator(".changes-raw summary").click();
  await page.locator(".changes-raw pre").getByText("invalid patch",{exact:true}).waitFor();
  // A detached task may finish its old request, but cannot update the next task.
  await page.evaluate(async () => {
    const {createExecutionChanges} = await import("/static/execution-changes.js?v=execution-changes-v1");
    const fetchOriginal = window.fetch;
    const waiting = [];
    window.fetch = () => new Promise(resolve => waiting.push(resolve));
    const controller = createExecutionChanges();
    const id = "detached";
    const makeEntry = (role, hash) => ({role,artifact_id:hash,content_url:`/api/v1/executions/${id}/artifacts/${hash}`});
    const promise = controller.update({id, artifact_manifests:[{stage:"implement",execution_attempt_id:id,
      manifest_hash:"c".repeat(64),entries:[makeEntry("unified-diff","a".repeat(64)),makeEntry("file-inventory","b".repeat(64))]}]});
    const previous = controller.summary.textContent;
    controller.dispose();
    waiting[0](new Response(""));
    waiting[1](new Response(JSON.stringify({changed_paths:[],baseline_inventory:[],result_inventory:[]}),{headers:{"content-type":"application/json"}}));
    try {
      await promise;
      if (controller.summary.textContent !== previous) throw new Error("Disposed task rendered stale results");
    } finally { window.fetch = fetchOriginal; }
  });
  assert.deepEqual(errors, []); assert.deepEqual(mutations, []);
  console.log("Execution change browser checks passed: summary, line numbers, polling, retry, empty/pending, pagination, safe text, responsive themes.");
} finally { await browser.close(); }
