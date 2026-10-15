// Real task, read-only browser acceptance: never authorize an external action.
import {createRequire} from "node:module";
import assert from "node:assert/strict";
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page = await browser.newPage();
const errors = [], writes = [];
page.on("pageerror", e => errors.push(e.message));
await page.route("**/api/**", route => {
  if (!["GET","HEAD","OPTIONS"].includes(route.request().method())) {
    writes.push(route.request().method()); return route.abort();
  }
  return route.continue();
});
try {
  const taskId = "82c0ec73-3be6-4499-b806-45cd31430722";
  await page.goto(`http://127.0.0.1:8000/#/tasks/${taskId}`);
  await page.getByRole("button",{name:"下载 PR 草稿",exact:true}).waitFor();
  const data = await (await page.request.get(`http://127.0.0.1:8000/api/v1/tasks/${taskId}/workbench`)).json();
  assert.equal(data.state,"ready");
  assert.ok(data.execution.acceptance.accepted);
  assert.equal(data.execution.acceptance.binding.attempt_id,"77dcfcdf-03bc-4f63-b7e3-52a0736dafd2");
  assert.equal(data.execution.attempts.length,9);
  assert.equal(data.execution.verification.passed,5);
  assert.equal(data.execution.blocked,null);
  assert.equal(data.busy,false);
  assert.equal(data.publisher_mode,"none");
  assert.equal(data.events.filter(e=>e.kind==="execution_accepted").length,1);
  assert.equal(data.events.some(e=>e.kind==="publication_confirmed"),false);
  assert.equal(await page.getByLabel("PR 标题").isVisible(),true);
  assert.ok((await page.getByLabel("PR 正文").inputValue()).includes("5/5 commands passed"));
  assert.equal(await page.locator(".studio-composer-actions").getByRole("button",{name:"发送",exact:true}).isEnabled(),true);
  for (const width of [1440,390]) {
    await page.setViewportSize({width,height:1000});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:`/tmp/contribos-acceptance-live-${width}.png`});
  }
  assert.deepEqual(errors,[]); assert.deepEqual(writes,[]);
  console.log("PASS: real hash-bound acceptance, five verified commands, nine preserved attempts, PR draft, desktop/mobile, zero API writes");
} finally { await browser.close(); }
