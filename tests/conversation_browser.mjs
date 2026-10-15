// Browser contract against the disposable fixture; all execution calls are intercepted.
import {createRequire} from "node:module";
import assert from "node:assert/strict";
import {diff, inventory, executionDetail} from "./execution_changes_fixtures.mjs";
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page = await browser.newPage({viewport:{width:1440,height:1000}});
const errors = [], mutations = [];
page.on("pageerror", e => errors.push(e.message));
try {
  await page.goto("http://127.0.0.1:8765");
  await page.locator("#sidebar-tasks a").first().click();
  await page.locator(".conversation-plan").waitFor();
  const taskId = (await page.evaluate(() => location.hash)).split("/").at(-1);
  const url = `http://127.0.0.1:8765/api/v1/tasks/${taskId}/workbench`;
  const data = await (await page.request.get(url)).json();
  const detail = {...executionDetail(), verification:{status:"environment_blocked",passed:0,total:1}};
  let executing = false;
  await page.route(url, route => route.fulfill({json: executing ? {...data, busy:true,state:"executing", jobs:[{id:"running",kind:"sandbox_stage",state:"running",created_at:new Date().toISOString()}], execution:{...data.execution, revision:"running",latest_attempt_id:detail.id,question_busy:false,actions:{can_replan:true},attempts:[{id:"previous-attempt",number:1,review:"not_run",verification:{status:"failed",passed:0,total:1,failures:[{command_id:"unit",exit_code:1,stderr:"",stdout:"AssertionError: historical failure"}]}},{id:detail.id,number:2,stage:"verify",stage_status:"succeeded",review:"not_run",verification:{status:"environment_blocked",passed:0,total:1,missing_dependencies:["filetype"],failures:[{command_id:"lint",exit_code:1,stderr:"ModuleNotFoundError: filetype"}]}}]}} : data}));
  await page.route(url + "/execute", route => {mutations.push({path:"execute",body:route.request().postDataJSON()});executing=true;return route.fulfill({status:202,json:{id:"start",state:"queued"}});});
  await page.route(url + "/messages", route => {mutations.push({path:"messages",body:route.request().postDataJSON()});return route.fulfill({status:202,json:{id:"question",state:"queued"}});});
  await page.route(url + "/replan", route => {mutations.push({path:"replan",body:route.request().postDataJSON()});return route.fulfill({status:202,json:{status:"stopping"}});});
  await page.route("**/api/v1/executions/*", route => route.fulfill({json:detail}));
  await page.route("**/api/v1/executions/*/artifacts/*", route => route.request().url().endsWith("a".repeat(64)) ? route.fulfill({contentType:"text/x-diff",body:diff}) : route.fulfill({json:inventory}));
  await page.reload();
  const plan = page.locator(".conversation-plan");
  assert.equal(await plan.evaluate(el=>el.closest('.ai-workbench-messages')!==null),true);
  await page.getByLabel("我批准当前方案", {exact:false}).check();
  await page.getByRole("button",{name:"确认方案并执行",exact:true}).click();
  await page.locator(".conversation-outcome").getByText("验证环境未就绪",{exact:false}).waitFor();
  const selector = page.getByLabel("查看执行轮次");
  await selector.selectOption("previous-attempt");
  await page.locator(".conversation-outcome").getByText("验证未通过",{exact:false}).waitFor();
  assert.ok((await page.locator(".conversation-outcome").textContent()).includes("historical failure"));
  await selector.selectOption("");
  await page.locator(".conversation-outcome").getByText("验证环境未就绪",{exact:false}).waitFor();
  const input = page.getByLabel("发送给贡献助手的消息");
  for (const width of [1440,390]) {
    await page.setViewportSize({width,height:900});
    const actions = page.locator(".studio-composer-actions");
    const stop = actions.getByRole("button",{name:"停止",exact:true});
    const send = actions.getByRole("button",{name:"询问进度",exact:true});
    assert.equal(await stop.isVisible(),true);
    assert.equal(await send.isEnabled(),true);
    const stopBox = await stop.boundingBox(), sendBox = await send.boundingBox();
    assert.ok(stopBox.x + stopBox.width <= sendBox.x);
    assert.equal(await page.getByRole("button",{name:"修改目标",exact:true}).isVisible(),false);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:`/tmp/contribos-composer-executing-${width}.png`});
  }
  await page.setViewportSize({width:1440,height:1000});
  await input.fill("为什么测试没有通过？");
  await page.getByRole("button",{name:"询问进度",exact:true}).click();
  assert.equal(mutations.at(-1).body.mode,"question");
  assert.equal(mutations.some(m=>m.path==="stop"),false);
  await page.locator(".changes-summary").getByRole("button",{name:"app/中文 file.py",exact:false}).click();
  await page.locator('.changes-file[data-path="app/中文 file.py"] .changes-add').first().waitFor();
  await input.fill("保留旧测试，先修复依赖准备");
  assert.equal(await page.getByRole("button",{name:"修改目标",exact:true}).isVisible(),false);
  await page.getByLabel("更多任务操作").click();
  await page.getByRole("button",{name:"修改目标",exact:true}).click();
  assert.equal(mutations.filter(m=>m.path==="replan").length,0);
  await page.getByRole("button",{name:"确认停止并重新规划",exact:true}).click();
  assert.equal(mutations.at(-1).path,"replan");
  // A stopped task with an unsupported environment must offer diagnosis, not
  // imply approval can start execution or silently regenerate a plan.
  await page.unroute(url);
  await page.route(url, route => route.fulfill({json:{...data, busy:false, state:"planning",
    execution:{...data.execution, revision:"unsupported", latest_attempt_id:null,
      last_attempt_id:detail.id, environment:{status:"unsupported",reason:"项目需要专用环境适配。"},
      actions:{can_execute:false,can_replan:false}, attempts:[{id:detail.id,number:2,
        verification:{status:"environment_blocked",passed:0,total:1,missing_dependencies:["filetype"]}}]}}}));
  await page.reload();
  await page.locator(".conversation-readiness").waitFor();
  assert.equal(await page.getByLabel("消息用途").inputValue(),"question");
  assert.equal(await page.getByRole("button",{name:"确认方案并执行",exact:true}).isDisabled(),true);
  assert.equal(await page.getByRole("button",{name:"确认方案并执行",exact:true}).getAttribute("title"),"项目需要专用环境适配。");
  await page.getByRole("button",{name:"查看上次执行",exact:true}).click();
  await page.locator(".conversation-outcome").getByText("验证环境未就绪",{exact:false}).waitFor();
  await page.getByRole("button",{name:"询问阻断原因",exact:true}).click();
  await page.getByRole("button",{name:"询问进度",exact:true}).click();
  assert.equal(mutations.at(-1).body.mode,"question");
  await page.getByLabel("消息用途").selectOption("planning");
  await input.fill("请调整方案中的实现步骤");
  await page.getByRole("button",{name:"发送",exact:true}).click();
  assert.equal(mutations.at(-1).body.mode,"planning");
  for (const width of [1440,390]) {
    await page.setViewportSize({width,height:900});
    assert.equal(await input.isVisible(),true);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:`/tmp/contribos-conversation-${width}.png`});
  }
  // Acceptance works with no Publisher and no planning model, while ordinary
  // result discussion remains a question rather than an implicit approval.
  let accepted = false;
  const acceptance = {available:true,binding_hash:"b".repeat(64),binding:{verify_result_hash:"c".repeat(64),review_hash:"d".repeat(64)}};
  await page.unroute(url);
  await page.route(url, route => route.fulfill({json:{...data, state:"ready",busy:false,publisher_mode:"none",planner_available:false,
    events:[...data.events,...(accepted ? [{id:"accepted",kind:"execution_accepted",payload:{text:"我确认当前结果",reply:"验收已保存，下一步准备 PR。"}}] : [])],
    execution:{...data.execution,revision:accepted ? "accepted" : "ready",latest_attempt_id:detail.id,
      actions:{can_accept:!accepted,can_replan:true},acceptance:{...acceptance,accepted:accepted ? {record_hash:"e".repeat(64)} : null},
      verification:{status:"passed",passed:5,total:5},attempts:[{id:detail.id,number:2,review:"pass",verification:{status:"passed",passed:5,total:5}}]}}}));
  await page.route(url + "/acceptance", route => {
    mutations.push({path:"acceptance",body:route.request().postDataJSON()});accepted=true;
    return route.fulfill({json:{accepted:true,record_hash:"e".repeat(64),binding_hash:acceptance.binding_hash}});
  });
  await page.reload();
  await page.getByRole("button",{name:"填写验收确认",exact:true}).click();
  assert.equal(await page.getByLabel("消息用途").inputValue(),"acceptance");
  await input.fill("我确认当前结果");
  const acceptButton = page.locator(".studio-composer-actions").getByRole("button",{name:"确认验收",exact:true});
  assert.equal(await acceptButton.isEnabled(),true);
  await acceptButton.click();
  await page.getByRole("button",{name:"下载 PR 草稿",exact:true}).waitFor();
  assert.equal(mutations.at(-1).path,"acceptance");
  assert.equal(mutations.at(-1).body.binding_hash,acceptance.binding_hash);
  assert.equal(mutations.at(-1).body.text,"我确认当前结果");
  assert.equal(await page.getByLabel("PR 标题").isVisible(),true);
  assert.ok((await page.getByLabel("PR 正文").inputValue()).includes("5/5 commands passed"));
  assert.equal(await page.getByRole("button",{name:"准备发布预览",exact:true}).count(),0);
  assert.equal(await page.locator(".studio-composer-actions").getByRole("button",{name:"发送",exact:true}).isVisible(),true);
  for (const width of [1440,390]) {
    await page.setViewportSize({width,height:900});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:`/tmp/contribos-acceptance-${width}.png`});
  }
  assert.deepEqual(errors,[]);
  console.log("PASS: inline approval, failed verification evidence, execution questions, explicit goal change, diff, blocked-task recovery, desktop/mobile");
} finally { await browser.close(); }
