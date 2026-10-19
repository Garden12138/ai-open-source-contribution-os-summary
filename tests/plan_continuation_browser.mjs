// Resource completion must lead to an explicit, current plan confirmation.
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser=await chromium.launch({headless:true});
const page=await browser.newPage({viewport:{width:1440,height:900}});
const errors=[], executions=[];
page.on('pageerror',e=>errors.push(e.message));
try {
  await page.goto('http://127.0.0.1:8765');
  await page.locator('#sidebar-tasks a').first().click();
  await page.locator('.conversation-agent-message').first().waitFor();
  const task=(await page.evaluate(()=>location.hash)).split('/').at(-1);
  const url=`http://127.0.0.1:8765/api/v1/tasks/${task}/workbench`;
  const source=await (await page.request.get(url)).json();
  let plan=source.plans.at(-1), plans=[...source.plans], state='planning', busy=false, pending=0, paused=false, reject=true;
  let model=source.model_binding_hash, context=source.context, preflight='e'.repeat(64), resource='f'.repeat(64);
  const snapshot=()=>({...source,state,busy,context,model_binding_hash:model,plans,planner_available:true,
    native_resources:{available:true,ready:true,status:'ready',receipt_hash:resource},
    execution:{...source.execution,preflight:{status:'ready',report_hash:preflight},actions:{can_execute:!busy&&state==='planning'},readiness:{blockers:[]}},
    conversation:{version:'conversation-v1',messages:[],results:[{id:'resource-completed',sequence:1000,
      text:'固定测试资源已下载并通过校验，请重新核对方案。'}],pending_count:pending,paused,
      pending_action:!busy&&!pending&&!paused&&state==='planning'?{kind:'execute',source_id:plan.id,requires_confirmation:true}:null,
      user_state:{label:'测试资源已就绪，等待确认方案',busy,can_stop:busy}}});
  await page.route(url,route=>route.fulfill({json:snapshot()}));
  await page.route(url+'/execute',route=>{
    executions.push(route.request().postDataJSON());
    if(reject){reject=false;return route.fulfill({status:503,json:{detail:'执行请求暂未保存，请重新提交。'}});}
    busy=true;state='executing';return route.fulfill({status:202,json:{id:'execution-job',state:'queued'}});
  });
  await page.reload();
  const review=page.getByRole('button',{name:'核对方案并继续',exact:true});
  await review.waitFor();
  assert.equal(await page.locator('.conversation-agent-messages > article').last().getByRole('button',{name:'核对方案并继续',exact:true}).count(),1);
  await review.click();
  const dialog=page.getByRole('dialog'), start=dialog.getByRole('button',{name:'确认方案并开始',exact:true});
  await start.waitFor();assert.equal(await start.isEnabled(),true);assert.equal(executions.length,0);
  assert.ok((await dialog.innerText()).includes(plan.goal));
  for(const path of plan.files_likely_to_change) assert.ok((await dialog.innerText()).includes(path));
  assert.ok((await dialog.innerText()).includes('先准备隔离环境并运行修改前测试'));
  for(const flag of ['pending','busy','paused']){
    if(flag==='pending') pending=1;
    if(flag==='busy') busy=true;
    if(flag==='paused') paused=true;
    await page.waitForFunction(()=>document.querySelector('dialog [data-confirmation]').disabled);
    pending=0;busy=false;paused=false;
    await page.waitForFunction(()=>!document.querySelector('dialog [data-confirmation]').disabled);
  }
  for(const change of ['model','preflight','resource','context']){
    if(change==='model') model='1'.repeat(64);
    if(change==='preflight') preflight='2'.repeat(64);
    if(change==='resource') resource='3'.repeat(64);
    if(change==='context') context={...context,record_hash:'4'.repeat(64)};
    await page.waitForFunction(()=>document.querySelector('dialog [data-confirmation]').disabled);
    assert.equal(executions.length,0);
    await dialog.getByRole('button',{name:'返回对话',exact:true}).click();await review.click();
    assert.equal(await start.isEnabled(),true);
  }
  plan={...plan,id:'new-plan',record_hash:'5'.repeat(64),goal:'Revised current plan'};plans=[...plans,plan];
  await dialog.getByText('这是历史方案，请返回对话核对最新方案。',{exact:true}).waitFor();
  assert.equal(await start.isDisabled(),true);
  await dialog.getByRole('button',{name:'返回对话',exact:true}).click();await review.click();
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:900});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:`/tmp/contribos-plan-continuation-${width}.png`});
  }
  await start.click();await dialog.getByRole('alert').filter({hasText:'暂未保存'}).waitFor();
  await page.waitForFunction(()=>!document.querySelector('dialog [data-confirmation]').disabled);
  await start.click();await dialog.waitFor({state:'hidden'});await review.waitFor({state:'hidden'});
  assert.deepEqual(executions,[1,2].map(()=>({plan_id:plan.id,plan_hash:plan.record_hash,approve_plan:true,
    start_execution:true,max_repairs:2,model_binding_hash:model,conversation:true})));
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({ok:true,latestAction:true,reviewDoesNotExecute:true,staleInputsRejected:true,
    confirmationRetries:2,desktop:true,mobile:true,realExecutions:0}));
}finally{await browser.close();}
