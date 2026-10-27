// Native UI regression; all mutations are intercepted, no model/GitHub writes.
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page = await browser.newPage({viewport:{width:1440,height:1000}});
const errors=[], calls=[];
page.on('pageerror', error => errors.push(error.message));
try {
  await page.goto('http://127.0.0.1:8765');
  await page.locator('#sidebar-tasks a').first().click();
  await page.locator('.conversation-agent-message').first().waitFor();
  const task = (await page.evaluate(() => location.hash)).split('/').at(-1);
  const url=`http://127.0.0.1:8765/api/v1/tasks/${task}/workbench`;
  const original=await (await page.request.get(url)).json();
  let reject=false;
  const source={id:'runtime-result',record_hash:'a'.repeat(64),sequence:9000,
    text:'第一项读取代码，第二项检查边界条件。',version:'conversation-v1',references:[]};
  const snapshot=()=>({...original,state:'ready',busy:false,
    execution:{...original.execution,actions:{can_accept:true},acceptance:{binding_hash:'b'.repeat(64),accepted:null}},
    conversation:{version:'conversation-v1',paused:false,pending_count:1,blocking_count:0,
      pending_action:{kind:'accept',requires_confirmation:true},
      user_state:{phase:'ready',label:'修改完成',next_step:'请核对结果。',busy:false,can_stop:false},
      messages:[],results:[source,{id:'runtime-accept',record_hash:'c'.repeat(64),sequence:9001,
        text:'测试与审查已完成。',source_kind:'awaiting_acceptance'}],
      observation:{version:'agent-runtime-v1',focus:null,foreground:[{kind:'conversation_intent',state:'running'}],background:[]}}});
  await page.route(url, route=>route.fulfill({json:snapshot()}));
  await page.route(url+'/conversation/messages', route=>{
    calls.push({body:route.request().postDataJSON(),key:route.request().headers()['idempotency-key']});
    if(reject){reject=false;return route.fulfill({status:409,json:{detail:'请重试发送。'}});}
    return route.fulfill({status:202,json:{message_id:'received',status:'queued',text:'已收到。'}});
  });
  await page.route(url+'/acceptance', route=>{
    calls.push({acceptance:route.request().postDataJSON()});
    return route.fulfill({json:{accepted:true,record_hash:'d'.repeat(64),binding_hash:'b'.repeat(64)}});
  });
  await page.reload();
  const input=page.getByRole('textbox',{name:'发送给贡献助手的消息',exact:true});
  const result=page.locator('article').filter({hasText:source.text});
  await result.getByRole('button',{name:'询问这条结果',exact:true}).click();
  await input.fill('第二项具体检查什么？');
  await page.reload();
  await page.getByRole('button',{name:'取消引用',exact:true}).waitFor();
  assert.equal(await input.inputValue(),'第二项具体检查什么？');
  reject=true;
  await page.getByRole('button',{name:'发送',exact:true}).click();
  await page.locator('.conversation-agent-error').filter({hasText:'请重试发送'}).waitFor();
  await page.getByRole('button',{name:'发送',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.conversation-agent-composer textarea').value==='');
  assert.deepEqual(calls[0],calls[1]);
  assert.equal(calls[1].body.reply_to_message_id,source.id);
  assert.equal(calls[1].body.reply_to_message_hash,source.record_hash);
  assert.equal(await page.getByRole('button',{name:'取消引用',exact:true}).count(),0);
  assert.equal(await page.getByRole('button',{name:'确认结果',exact:true}).isEnabled(),true);
  await page.getByRole('button',{name:'确认结果',exact:true}).click();
  await page.waitForTimeout(200);
  assert.equal(calls.at(-1).acceptance.binding_hash,'b'.repeat(64));
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:1000});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:`/tmp/contribos-agent-runtime-${width}.png`});
  }
  assert.deepEqual(errors,[]);
  console.log('PASS: reply binding/reload/retry, foreground queries preserve confirmation, desktop/mobile');
} finally {await browser.close();}
