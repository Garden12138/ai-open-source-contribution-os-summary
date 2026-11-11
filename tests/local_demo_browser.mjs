// Optional real-browser smoke. All requests are restricted to an owned Fake demo.
import {createRequire} from "node:module";
import assert from "node:assert/strict";
import {writeFileSync} from "node:fs";
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || "playwright");
const origin = process.env.CONTRIBOS_BROWSER_ORIGIN || "http://127.0.0.1:18765";
assert.match(origin, /^http:\/\/127\.0\.0\.1:\d+$/);
const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page = await browser.newPage({viewport:{width:1440,height:1000}});
const errors = [], checks = [], external = [];
page.on("pageerror", error => errors.push(error.message));
await page.route("**/*", route => {
  if (new URL(route.request().url()).origin === origin) return route.continue();
  external.push(route.request().url());
  return route.abort();
});
try {
  const identity = await (await page.request.get(origin + "/api/local-demo")).json();
  assert.equal(identity.mode, "contribos-offline-demo-v1");
  await page.goto(origin);
  await page.locator("#opportunity-list .studio-card-title").first().waitFor();
  assert.match(await page.locator("body").innerText(), /离线试用/);
  checks.push("dashboard_and_demo_notice");
  await page.locator("#scan-button").click();
  await page.waitForFunction(() => !document.querySelector("#scan-button").disabled &&
    document.querySelector("#candidate-count").textContent === "1");
  checks.push("scan_via_web");
  await page.locator("#opportunity-list .studio-card-title").first().click();
  await page.locator("#opportunity-detail-content .opportunity-card").waitFor();
  assert.match(await page.locator("#opportunity-detail-content").innerText(), /离线样例/);
  checks.push("opportunity_details");
  await page.locator("#detail-close").click();
  const add = page.locator("#opportunity-list").getByRole("button", {name:"加入候选",exact:true});
  if (await add.count()) await add.click();
  await page.locator('[data-view-link="shortlist"]').click();
  await page.locator("#shortlist-list .opportunity-card").first().waitFor();
  checks.push("shortlist_mutation_and_view");
  await page.locator('[data-view-link="settings"]').click();
  await page.getByRole("heading", {name:"模型设置",exact:true}).waitFor();
  checks.push("model_settings_view");
  await page.locator('[data-view-link="contributions"]').click();
  await page.locator("#task-history").getByText(/离线样例/).first().waitFor();
  checks.push("task_history");
  await page.locator("#sidebar-tasks a").first().click();
  await page.locator("#studio-task-root").getByRole("heading").first().waitFor();
  await page.waitForFunction(() => {
    const status = document.querySelector("#studio-task-root .conversation-agent-status");
    return status && !status.textContent.includes("正在读取任务");
  });
  assert.equal((await page.locator("#studio-task-root").innerText()).includes("任务载入失败"), false);
  checks.push("task_detail");
  await page.locator("#studio-task-root").getByRole("button", {name:"历史记录",exact:true}).click();
  await page.getByRole("dialog").getByRole("button", {name:/查看第 .* 次尝试/}).first().click();
  await page.getByRole("dialog").getByText("审查通过", {exact:true}).waitFor();
  await page.getByRole("dialog").getByRole("heading", {name:"修改后测试",exact:true}).waitFor();
  checks.push("coding_verification_and_review_result_view");
  for (const width of [1440,390]) {
    await page.setViewportSize({width,height:1000});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  }
  checks.push("desktop_and_mobile_layout");
  assert.deepEqual(errors, []);
  assert.deepEqual(external, []);
  checks.push("no_page_errors_or_external_requests");
  const result = {status:"PASS",mode:identity.mode,checks,passed:checks.length,failed:0,
    completed_at:new Date().toISOString(),real_ai:false};
  if (process.env.CONTRIBOS_BROWSER_RESULT) writeFileSync(process.env.CONTRIBOS_BROWSER_RESULT, JSON.stringify(result,null,2)+"\n");
  console.log(JSON.stringify(result));
} finally {
  await page.unrouteAll({behavior:"wait"});
  await browser.close();
}
