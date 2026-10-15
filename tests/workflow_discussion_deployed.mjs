// Read-only evidence of the live snapshot -> isolated context -> model conversation.
import {createRequire} from "node:module";
import assert from "node:assert/strict";
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || "playwright");
const taskId=process.env.CONTRIBOS_DISCUSSION_TASK_ID || "e380cc10-fb6c-49f8-810a-11233e7929ad";
const base="http://127.0.0.1:8000";
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
const errors=[],mutations=[];
page.on("pageerror",e=>errors.push(e.message));
page.on("request",r=>{if(!["GET","HEAD","OPTIONS"].includes(r.method()))mutations.push(r.url());});
try {
  const data=await (await page.request.get(`${base}/api/v1/tasks/${taskId}/workbench`)).json();
  assert.ok(data.context);
  assert.ok(data.events.some(e=>e.kind==="assistant_message"));
  assert.ok(data.jobs.every(j=>j.state==="succeeded"));
  assert.ok(data.jobs.every(j=>["planning_archive","planning_context","planning_turn"].includes(j.kind)));
  assert.equal(data.plans.length,0);
  assert.equal(data.execution.attempts.length,0);
  await page.goto(base);
  await page.evaluate(id=>{location.hash=`#/tasks/${id}`;},taskId);
  await page.getByLabel("发送给贡献助手的消息").waitFor();
  await page.locator(".ai-message.assistant_message").first().waitFor();
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:1000});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:`/tmp/contribos-live-discussion-${width}.png`});
  }
  assert.deepEqual(errors,[]);assert.deepEqual(mutations,[]);
  console.log(JSON.stringify({status:"PASS",task_id:taskId,context_ready:true,real_conversation:true,plans:0,executions:0,mutations:0}));
} finally {await browser.close();}
