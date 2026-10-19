// Offline contract: deterministic continuation is independent of chat formatting.
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser=await chromium.launch({headless:true});
const page=await browser.newPage({viewport:{width:1440,height:900}}), errors=[],requests=[];
page.on('pageerror',e=>errors.push(e.message));
try {
  await page.goto('http://127.0.0.1:8765');
  await page.locator('#sidebar-tasks a').first().click();
  await page.locator('.conversation-agent-message').first().waitFor();
  const task=(await page.evaluate(()=>location.hash)).split('/').at(-1);
  const url=`http://127.0.0.1:8765/api/v1/tasks/${task}/workbench`;
  const source=await (await page.request.get(url)).json(), binding='b'.repeat(64);
  let phase='prepare_publication',busy=false,pending=0,paused=false,reject=true;
  const snapshot=()=>({...source,state:'ready',busy,
    native_resources:{available:true,ready:true,status:'ready'},
    execution:{...source.execution,actions:{can_accept:false}},
    conversation:{version:'conversation-v1',messages:[],results:[],pending_count:pending,paused,
      pending_action:!busy?{kind:phase,binding_hash:binding,requires_confirmation:false}:null,
      user_state:{label:busy?'正在读取贡献规范':'结果已确认',busy,can_stop:busy}}});
  await page.route(url,route=>route.fulfill({json:snapshot()}));
  await page.route(url+'/publication/continue',route=>{
    requests.push({body:route.request().postDataJSON(),key:route.request().headers()['idempotency-key']});
    if(reject){reject=false;return route.fulfill({status:503,json:{detail:'暂未保存，请重试。'}});}
    busy=true;return route.fulfill({json:{message_id:'continued',status:'queued',text:'会继续处理。'}});
  });
  await page.reload();
  const action=page.getByRole('button',{name:'继续准备 PR 文案和预览',exact:true});
  await action.waitFor();
  assert.equal(await page.getByRole('button',{name:'核对方案并继续',exact:true}).count(),0);
  assert.ok(!(await page.locator('.conversation-agent-messages').innerText()).includes('再单独确认执行'));
  for(const state of ['pending','paused','busy']){
    if(state==='pending')pending=1;
    if(state==='paused')paused=true;
    if(state==='busy')busy=true;
    if(state==='busy')await action.waitFor({state:'hidden'});
    else await page.waitForFunction(()=>document.querySelector('[data-confirmation]')?.disabled);
    pending=0;paused=false;busy=false;
    await action.waitFor();await page.waitForFunction(()=>!document.querySelector('[data-confirmation]').disabled);
  }
  phase='publisher_setup';await page.getByRole('link',{name:'前往设置',exact:true}).waitFor();
  assert.equal(await action.count(),0);
  phase='prepare_publication';await action.waitFor();
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:900});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:`/tmp/contribos-continuation-${width}.png`});
  }
  await action.click();await page.getByRole('alert').filter({hasText:'暂未保存'}).waitFor();
  await action.click();await action.waitFor({state:'hidden'});
  assert.equal(requests.length,2);assert.deepEqual(requests[0].body,{binding_hash:binding});
  assert.deepEqual(requests[1],requests[0]);assert.deepEqual(errors,[]);
  console.log(JSON.stringify({ok:true,deterministicContinuation:true,staleStateDisabled:true,
    accountSetup:true,retryKeyPreserved:true,desktop:true,mobile:true,modelCalls:0,realWrites:0}));
}finally{await browser.close();}
