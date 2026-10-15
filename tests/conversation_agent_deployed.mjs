// Read-only smoke check of the deployed historical task; never sends a message.
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser=await chromium.launch({headless:true});
const page=await browser.newPage({viewport:{width:1440,height:960}});
const task='82c0ec73-3be6-4499-b806-45cd31430722',origin='http://127.0.0.1:8000';
const errors=[],mutations=[];page.on('pageerror',e=>errors.push(e.message));
try {
  await page.route('**/*',route=>{
    if(!['GET','HEAD','OPTIONS'].includes(route.request().method())){mutations.push(route.request().url());return route.abort();}
    return route.continue();
  });
  const url=`${origin}/api/v1/tasks/${task}/workbench`;
  const before=await (await page.request.get(url)).json();
  const list=await (await page.request.get(`${origin}/api/v1/tasks`)).json();
  assert.equal(list.find(t=>t.id===task).friendly_state,before.conversation.user_state.label);
  assert.equal(before.conversation.pending_count,0);
  assert.equal(before.conversation.user_state.label,'自动检查等待维护者批准');
  const baseline=x=>JSON.stringify({events:x.events.map(e=>[e.id,e.record_hash]),jobs:x.jobs.map(j=>[j.id,j.state,j.attempt_count]),messages:x.conversation.messages});
  await page.goto(`${origin}/#/tasks/${task}`);
  const shell=page.locator('.conversation-agent');await shell.waitFor();
  await shell.getByText('自动检查等待维护者批准',{exact:false}).first().waitFor();
  assert.equal(await shell.locator('select').count(),0);assert.equal(await shell.locator('.conversation-agent-messages details').count(),0);
  assert.equal(await shell.getByRole('textbox').count(),1);
  assert.doesNotMatch(await shell.innerText(),/\b[a-f0-9]{40,64}\b|哈希/);
  await shell.getByRole('button',{name:'查看项目检查与反馈',exact:true}).click();
  const drawer=page.getByRole('dialog');await drawer.waitFor();
  assert.match(await drawer.innerText(),/批准|维护者/);await page.keyboard.press('Escape');
  for(const width of [1440,390]) {
    await page.setViewportSize({width,height:900});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.screenshot({path:`/tmp/contribos-conversation-deployed-${width}.png`});
  }
  const after=await (await page.request.get(url)).json();assert.equal(baseline(after),baseline(before));
  assert.deepEqual(errors,[]);assert.deepEqual(mutations,[]);
  console.log(JSON.stringify({status:'PASS',version:after.conversation.version,label:after.conversation.user_state.label,observed_at:after.conversation.user_state.observed_at,events:after.events.length,jobs:after.jobs.length,mutations:0,history_unchanged:true,viewports:[1440,390]}));
}finally{await browser.close();}
