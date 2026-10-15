// Real local API mutations against a disposable discovery fixture; no model or execution calls.
import {createRequire} from "node:module";
import assert from "node:assert/strict";
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || "playwright");
const base = process.env.CONTRIBOS_BROWSER_BASE || "http://127.0.0.1:8765";
const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page = await browser.newPage({viewport:{width:1440,height:1000}});
const errors=[], writes=[];
page.on("pageerror", error => errors.push(error.message));
page.on("request", request => {if (!["GET","HEAD","OPTIONS"].includes(request.method())) writes.push({url:request.url(), body:request.postDataJSON()});});
try {
  const feed = await (await page.request.get(`${base}/api/v1/recommendations`)).json();
  const tasks = await (await page.request.get(`${base}/api/v1/tasks`)).json();
  const existing = new Set(tasks.map(task => task.opportunity_id));
  const item = feed.items.find(item => !existing.has(item.opportunity.id) && !item.analysis_version_id && item.opportunity.repository.full_name.startsWith("fixture/evidence"));
  assert.ok(item,"A verified, unassessed snapshot fixture must exist");
  await page.goto(`${base}/#/discover`);
  await page.waitForFunction(()=>document.querySelector("#small-change-toggle").textContent.includes("("));
  await page.evaluate(id=>{location.hash=`#/opportunities/${id}`;}, item.opportunity.id);
  const detail=page.locator("#opportunity-detail-content");
  const start=detail.getByRole("button",{name:"开始对话制定方案 →",exact:true});
  await start.waitFor();
  assert.equal(await start.isEnabled(),true);
  await start.click();
  await page.waitForFunction(()=>location.hash.startsWith("#/tasks/"));
  await page.getByLabel("发送给贡献助手的消息").waitFor();
  const taskId=(await page.evaluate(()=>location.hash)).split("/").at(-1);
  const data=await (await page.request.get(`${base}/api/v1/tasks/${taskId}/workbench`)).json();
  const taskDetail = await (await page.request.get(`${base}/api/v1/tasks/${taskId}`)).json();
  assert.equal(taskDetail.task.snapshot_id,item.snapshot_id);
  assert.equal(data.plans.length,0);
  assert.equal(data.jobs.length,0);
  assert.equal(writes.length,1);
  assert.deepEqual(writes[0].body,{snapshot_id:item.snapshot_id});
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:1000});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:`/tmp/contribos-snapshot-discussion-${width}.png`});
  }
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({status:"PASS",task_id:taskId,optional_report:true,model_jobs:0,execution_jobs:0,browser_errors:0}));
} finally {await browser.close();}
