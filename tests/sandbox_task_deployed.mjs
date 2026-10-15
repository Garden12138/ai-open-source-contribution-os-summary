// Read-only acceptance of the recovered sandbox task; never clicks execute.
import {createRequire} from "node:module";
import assert from "node:assert/strict";
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||"playwright");
const task=process.env.CONTRIBOS_TASK_ID||"82c0ec73-3be6-4499-b806-45cd31430722";
const base="http://127.0.0.1:8000";
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
const errors=[],mutations=[];
page.on("pageerror",e=>errors.push(e.message));
page.on("request",r=>{if(!["GET","HEAD","OPTIONS"].includes(r.method()))mutations.push(r.url());});
try{
 const data=await (await page.request.get(`${base}/api/v1/tasks/${task}/workbench`)).json();
 const plan=data.plans.at(-1),receipt=data.events.filter(e=>e.kind==="assistant_message").at(-1).payload;
 assert.ok(plan.parent_version_id);
 assert.equal(receipt.plan_receipt.plan_id,plan.id);
 assert.equal(receipt.plan_receipt.execution_started,false);
 assert.equal(data.execution.environment.mode,"workspace-v2");
 assert.equal(data.execution.actions.can_execute,true);
 assert.equal(data.execution.attempts.length,0);
 assert.ok(!plan.commands_to_run.some(c=>c.argv.includes("install")));
 await page.goto(base);
 await page.evaluate(id=>{location.hash=`#/tasks/${id}`;},task);
 await page.locator(".conversation-plan").waitFor();
 const execute=page.getByRole("button",{name:"确认方案并执行",exact:true});
 await execute.waitFor();
 const approval=page.getByLabel(/我批准当前方案，在沙箱准备环境/);
 await approval.check();
 assert.equal(await execute.isEnabled(),true);
 for(const width of [1440,390]){
  await page.setViewportSize({width,height:1000});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.screenshot({path:`/tmp/contribos-recovered-sandbox-${width}.png`});
 }
 assert.deepEqual(errors,[]);assert.deepEqual(mutations,[]);
 console.log(JSON.stringify({status:"PASS",task,plan_id:plan.id,execution_enabled_after_local_confirmation:true,attempts:0,mutations:0}));
}finally{await browser.close();}
