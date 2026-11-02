// Intercepted browser contracts only; no real model or sandbox execution.
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin=process.env.CONTRIBOS_BROWSER_ORIGIN || 'http://127.0.0.1:18769';
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page=await browser.newPage();
const errors=[], executions=[];
page.on('pageerror',e=>errors.push(e.message));
try {
  await page.goto(origin);await page.locator('#sidebar-tasks a').first().click();
  await page.locator('.conversation-agent-message').first().waitFor();
  const task=(await page.evaluate(()=>location.hash)).split('/').at(-1);
  const url=`${origin}/api/v1/tasks/${task}/workbench`;
  const source=await (await page.request.get(url)).json(), plan=source.plans.at(-1);
  let hash='a'.repeat(64),busy=false;
  const snapshot=()=>({...source,state:'planning',busy,planner_available:true,
    execution:{...source.execution,model_transport:{version:'agent-native-tools-v1',tools_schema_hash:'c'.repeat(64)},
      model_transport_hash:hash,actions:{can_execute:!busy},readiness:{blockers:[]}},
    conversation:{version:'conversation-v1',messages:[],results:[],pending_count:0,paused:false,
      pending_action:{kind:'execute',source_id:plan.id,requires_confirmation:true},
      user_state:{label:'等待确认方案',busy,can_stop:busy}}});
  await page.route(url,route=>route.fulfill({json:snapshot()}));
  await page.route(url+'/execute',route=>{
    executions.push(route.request().postDataJSON());busy=true;
    return route.fulfill({status:202,json:{id:'fixture-execution',state:'queued'}});
  });
  await page.reload();
  const review=page.getByRole('button',{name:'核对方案并继续',exact:true});
  await review.click();
  const dialog=page.getByRole('dialog'),start=dialog.getByRole('button',{name:'确认方案并开始',exact:true});
  assert.match(await dialog.innerText(),/受限原生工具接口/);
  assert.equal(executions.length,0);
  hash='b'.repeat(64);
  await page.waitForFunction(()=>document.querySelector('dialog [data-confirmation]').disabled);
  assert.equal(executions.length,0);
  await dialog.getByRole('button',{name:'返回对话',exact:true}).click();await review.click();
  assert.equal(await start.isEnabled(),true);
  for(const width of [1440,390]) {
    await page.setViewportSize({width,height:900});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  }
  await start.click();await dialog.waitFor({state:'hidden'});
  assert.equal(executions.length,1);
  assert.equal(executions[0].model_transport_hash,hash);
  assert.equal(executions[0].plan_hash,plan.record_hash);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({classification:'intercepted_browser_contract_not_native',stale_transport_disabled:true,
    exact_hash_submitted:true,viewports:2,real_executions:0,passed:true}));
} finally {await browser.close();}
