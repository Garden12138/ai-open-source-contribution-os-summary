// Offline regression for relocating task model selection outside the composer.
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser=await chromium.launch({headless:true});
const page=await browser.newPage();
const errors=[];page.on('pageerror',e=>errors.push(e.message));
try {
  await page.goto('http://127.0.0.1:8765/health');
  await page.setContent('<main id="settings"></main>');
  let profiles={analysis:'p1',planning:'p1',implementation:'p1',review:'p1'},busy=false,writes=[];
  await page.route('**/api/v1/model-settings',r=>r.fulfill({json:{connections:[{id:'c1',scope:'connection:c',payload:{name:'连接',base_url:'https://example.invalid',provider:'openai_compatible'}}],
    profiles:['p1','p2'].map(id=>({id,scope:'profile:'+id,payload:{name:id==='p1'?'模型甲':'模型乙',model:id,connection_id:'c1',max_tokens:8192}})),version:{record_hash:'global',payload:{default:'p1'}},credential_management_available:false}}));
  await page.route('**/api/v1/tasks',r=>r.fulfill({json:[{id:'task',repository_full_name:'fixture/repo',opportunity_title:'修复问题'}]}));
  await page.route('**/api/v1/tasks/task/workbench',r=>r.fulfill({json:{state:'planning',busy,model_profiles:profiles,model_binding_hash:'binding',conversation:{pending_count:0,paused:false}}}));
  await page.route('**/api/v1/tasks/task/workbench/models',r=>{
    const body=r.request().postDataJSON();writes.push(body);profiles=body.profiles;return r.fulfill({json:{changed:true,record_hash:'new-binding'}});
  });
  const mount=()=>page.evaluate(async()=>{
    const {mountModelSettings}=await import('/static/model-settings.js?v=conversation-agent-v1');
    mountModelSettings(document.querySelector('#settings'),{ensureAccess:async()=>{},headers:extra=>extra});
  });
  await mount();
  const section=page.locator('section').filter({has:page.getByRole('heading',{name:'单个任务的模型',exact:true})});
  await section.getByRole('combobox',{name:'选择任务',exact:true}).selectOption('task');
  const save=section.getByRole('button',{name:'应用到此任务',exact:true});await save.waitFor();assert.equal(await save.isDisabled(),true);
  await section.getByRole('combobox',{name:'方案讨论',exact:true}).selectOption('p2');assert.equal(await save.isEnabled(),true);await save.click();
  await section.getByRole('status').filter({hasText:'任务模型已更新'}).waitFor();
  assert.deepEqual(writes,[{expected_hash:'binding',profiles:{analysis:'p1',planning:'p2',implementation:'p1',review:'p1'}}]);
  busy=true;await mount();await section.getByRole('combobox',{name:'选择任务',exact:true}).selectOption('task');
  await section.getByRole('combobox',{name:'方案讨论',exact:true}).waitFor();assert.equal(await section.getByRole('combobox',{name:'方案讨论',exact:true}).isDisabled(),true);
  assert.equal(writes.length,1);assert.deepEqual(errors,[]);
  console.log('PASS: task models in Settings, unchanged selection disabled, exact existing API binding, busy task locked');
}finally{await browser.close();}
