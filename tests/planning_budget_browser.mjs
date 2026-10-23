// Disposable fixture: budget changes never send chat, invoke a model or execute.
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser=await chromium.launch({headless:true});
const page=await browser.newPage({viewport:{width:1440,height:960}}), errors=[],mutations=[];
page.on('pageerror',error=>errors.push(error.message));
try {
  await page.goto('http://127.0.0.1:8765');
  await page.locator('#sidebar-tasks a').first().click();
  const selector=page.getByRole('combobox',{name:'本任务累计输入预算'});
  await selector.waitFor();
  const task=(await page.evaluate(()=>location.hash)).split('/').at(-1);
  const url=`http://127.0.0.1:8765/api/v1/tasks/${task}/workbench`;
  const source=await (await page.request.get(url)).json();
  let revision='a'.repeat(64),busy=false,reject=false,cap=2000000,calls=32,outputs=100000;
  const budget=()=>({...source.execution.planning_budget,revision,
    limits:{calls,input_tokens:cap,output_tokens:outputs},configurable:!busy,
    usage:{calls:23,input_tokens:2079795,output_tokens:52195}});
  const snapshot=()=>({...source,busy,execution:{...source.execution,planning_budget:budget()}});
  await page.route(url,route=>route.fulfill({json:snapshot()}));
  await page.route(url+'/budget',route=>{
    const request={body:route.request().postDataJSON(),key:route.request().headers()['idempotency-key']};
    mutations.push(request);
    if(reject){reject=false;return route.fulfill({status:503,json:{detail:'暂未保存，请重试。'}});}
    assert.equal(request.body.expected_hash,revision);
    cap=request.body.limits.input_tokens;calls=request.body.limits.calls;outputs=request.body.limits.output_tokens;
    revision=(revision==='a'.repeat(64)?'b':'c').repeat(64);
    return route.fulfill({json:budget()});
  });
  await page.route(url+'/conversation/messages',()=>{throw new Error('Budget selection must not send a conversation message');});
  await page.reload();await selector.waitFor();
  await page.getByLabel('发送给贡献助手的消息').fill('预算调整后保留这段草稿');
  assert.equal(await selector.locator('option[value="2000000"]').isDisabled(),true);
  await selector.selectOption('3000000');
  await page.waitForFunction(()=>document.querySelector('.conversation-budget-control select')?.value==='3000000'
    && !document.querySelector('.conversation-budget-control select')?.disabled);
  assert.equal(mutations.length,1);
  assert.deepEqual(mutations[0].body.limits,{calls:32,input_tokens:3000000,output_tokens:100000});
  assert.equal(await page.getByLabel('发送给贡献助手的消息').inputValue(),'预算调整后保留这段草稿');
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:960});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    assert.equal(await selector.isVisible(),true);
    await page.screenshot({path:`/tmp/contribos-budget-dropdown-${width}.png`});
  }
  busy=true;
  await page.waitForFunction(()=>document.querySelector('.conversation-budget-control select')?.disabled);
  busy=false;
  await page.waitForFunction(()=>!document.querySelector('.conversation-budget-control select')?.disabled);
  reject=true;await selector.selectOption('5000000');
  await page.getByRole('alert').filter({hasText:'暂未保存'}).waitFor();
  assert.equal(await selector.inputValue(),'3000000');
  await selector.selectOption('5000000');
  await page.waitForFunction(()=>document.querySelector('.conversation-budget-control select')?.value==='5000000'
    && !document.querySelector('.conversation-budget-control select')?.disabled);
  assert.deepEqual(mutations[1],mutations[2]);
  await selector.selectOption('custom');
  const save=page.getByRole('button',{name:'保存当前任务预算',exact:true});
  await save.waitFor();
  await page.getByRole('spinbutton',{name:'累计输入 token',exact:true}).fill('6000000');
  await page.getByRole('spinbutton',{name:'累计输出 token',exact:true}).fill('150000');
  await page.getByRole('spinbutton',{name:'累计调用次数',exact:true}).fill('40');
  await save.click();
  await page.getByRole('dialog').waitFor({state:'hidden'});
  assert.deepEqual(mutations.at(-1).body.limits,{calls:40,input_tokens:6000000,output_tokens:150000});
  await selector.selectOption('custom');await save.waitFor();
  revision='d'.repeat(64);
  await page.waitForFunction(()=>document.querySelector('[data-budget-save]')?.disabled);
  await page.getByRole('status').filter({hasText:'预算或用量已变化'}).waitFor();
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({ok:true,dropdown:true,custom:true,desktop:true,mobile:true,
    draftPreserved:true,busyDisabled:true,staleDisabled:true,idempotentRetry:true,
    modelCalls:0,realWrites:0}));
}finally{await browser.close();}
