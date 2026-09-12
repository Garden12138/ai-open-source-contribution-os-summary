// Read-only smoke check against the existing deployment; never submits a form.
import {createRequire} from "node:module";
import assert from "node:assert/strict";
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
const errors=[];
page.on("pageerror",error=>errors.push(error.message));
const mutations=[];
page.on("request",request=>{if(!["GET","HEAD","OPTIONS"].includes(request.method()))mutations.push(request.method());});
try {
  await page.goto("http://127.0.0.1:8000");
  await page.locator(".studio-card-title").first().waitFor();
  assert.equal(await page.locator("#app-name").textContent(),"Contribution Studio");
  await page.locator(".studio-card-title").first().click();
  await page.locator("#opportunity-detail .opportunity-card").waitFor();
  await page.keyboard.press("Escape");
  await page.locator("#opportunity-detail").waitFor({state:"hidden"});
  await page.screenshot({path:"/tmp/contribos-live-studio-discover.png",fullPage:false});
  const activeTasks=await (await page.request.get("http://127.0.0.1:8000/api/v1/tasks")).json();
  let taskRoute="#/contributions";
  if(activeTasks.length) {
  const taskLink=process.env.STUDIO_TASK_ID
    ? page.locator(`#sidebar-tasks a[href="#/tasks/${process.env.STUDIO_TASK_ID}"]`)
    : page.locator("#sidebar-tasks a").first();
  await taskLink.click();
  await page.getByLabel("发送给规划助手的消息").waitFor();
  await page.locator(".ai-workbench-messages > .ai-workbench-progress").waitFor();
  assert.equal(await page.locator(".ai-workbench > .ai-workbench-progress").count(),0);
  assert.equal(await page.locator(".ai-message.user_message > strong").count(),0);
  assert.equal(await page.locator(".ai-workbench-tabs, .studio-chat-tools").count(),0);
  assert.equal(await page.getByRole("button",{name:"重新读取上游代码",exact:true}).count(),0);
  await page.getByLabel("更多任务操作").click();
  await page.getByRole("button",{name:"归档任务",exact:true}).waitFor();
  await page.getByRole("button",{name:"任务模型设置",exact:true}).waitFor();
  await page.getByRole("button",{name:"任务模型设置",exact:true}).click();
  await page.getByLabel("本任务实现模型",{exact:true}).waitFor();
  assert.equal(await page.getByRole("button",{name:"应用到此任务",exact:true}).isDisabled(),true);
  await page.getByRole("button",{name:"返回对话",exact:true}).click();
  assert.equal(await page.locator('link[href*="execution-changes.css"]').count(),1);
  if(process.env.STUDIO_CHECK_CHANGES==="1") {
    const changes=page.locator(".changes-summary:not([hidden])");
    await changes.getByText("已修改",{exact:false}).waitFor();
    await changes.locator(".changes-file-list .changes-button").first().click();
    await page.locator(".changes-viewer .changes-file").first().waitFor();
    await page.getByRole("button",{name:"返回对话",exact:true}).click();
  }
  if(process.env.STUDIO_CHECK_CANCELLED==="1") {
    await page.locator(".ai-workbench-error").filter({hasText:"本次请求已取消"}).waitFor();
    assert.equal(await page.getByRole("button",{name:"发送",exact:true}).isEnabled(),true);
  }
  taskRoute=await page.evaluate(()=>location.hash);
  await page.screenshot({path:"/tmp/contribos-live-studio-task.png",fullPage:false});
  } else {
    assert.equal(await page.locator("#sidebar-tasks a").count(),0);
  }
  await page.locator('[data-view-link="settings"]').click();
  await page.getByLabel("API Key",{exact:true}).waitFor();
  assert.equal(await page.getByLabel("API Key",{exact:true}).isEnabled(),true);
  assert.equal(await page.getByLabel("API Key",{exact:true}).inputValue(),"");
  await page.screenshot({path:"/tmp/contribos-live-studio-settings.png",fullPage:false});
  await page.locator('[data-view-link="contributions"]').click();
  await page.getByLabel("查看已归档任务").check();
  await page.locator('#task-history[aria-busy="false"]').waitFor();
  const archived=await (await page.request.get("http://127.0.0.1:8000/api/v1/tasks?archived=true")).json();
  assert.equal(await page.locator("#task-history .task-history-item").count(),archived.length);
  await page.getByLabel("查看已归档任务").uncheck();
  await page.setViewportSize({width:390,height:844});
  for(const route of ["#/discover",taskRoute,"#/settings","#/contributions"]) {
    await page.evaluate(hash=>location.hash=hash,route);
    await page.waitForTimeout(400);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  }
  assert.deepEqual(errors,[]);
  assert.deepEqual(mutations,[]);
  console.log("Live Studio passed: cards/details, restored task chat, archive controls/list, enabled settings with no key echo, mobile layout; GET-only, no JavaScript errors.");
} finally {await browser.close();}
