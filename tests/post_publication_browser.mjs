// Offline rendering contract; the Python tests exercise the real coordinator.
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],writes=[];
page.on('pageerror',e=>errors.push(e.message));
page.on('request',r=>{if(!['GET','HEAD'].includes(r.method()))writes.push(r.url());});
try {
  await page.goto('http://127.0.0.1:8765');
  await page.locator('#sidebar-tasks a').first().click();
  await page.locator('.conversation-agent-status').waitFor();
  const task=(await page.evaluate(()=>location.hash)).split('/').at(-1);
  const url=`http://127.0.0.1:8765/api/v1/tasks/${task}/workbench`;
  const source=await (await page.request.get(url)).json(),account='b'.repeat(64);
  const before={title:'fix: preserve streamed output',body:'## Summary\nPreserve all fragments.',draft:true,
    state:'open',merged:false,head_sha:'c'.repeat(40),base_sha:'d'.repeat(40)};
  const preview={record_hash:'a'.repeat(64),target:{pr_url:'https://github.com/fixture/repo/pull/7'},
    before,publisher_account_hash:account,login:'contributor',nonce:'e'.repeat(64),expires_at:'2099-01-01T00:00:00Z',
    acknowledgement:'确认将当前精确 PR 提交正式审核。',consumed:false};
  const snapshot={pr:before,ci:'awaiting_approval',stage:'draft',next_actor:'author',
    next_action:'请查看审核预览并单独确认。',observed_at:'2026-10-23T08:00:00Z',
    target:preview.target,checks:[],workflows:[],statuses:[],reviews:[],comments:[],warnings:[]};
  const events=[...source.events,
    {id:'published',sequence:100,kind:'publication_completed',payload:{html_url:preview.target.pr_url}},
    {id:'manual-source',sequence:101,kind:'pr_ready_prepared',payload:{}},
    {id:'auto-observation',sequence:103,kind:'pr_followup_observed',payload:snapshot},
    {id:'auto-preview',sequence:104,kind:'pr_ready_prepared',record_hash:preview.record_hash,payload:preview}];
  const result=(id,sequence,source_id,source_kind,text,kind,label)=>({id,sequence,source_id,source_kind,text,origin:{kind,label}});
  const results=[
    result('manual-result',102,'manual-source','pr_ready_prepared','此前维护操作准备过审核预览。','maintenance','维护操作触发'),
    result('auto-result',105,'auto-observation','pr_followup_observed','远端自动检查等待维护者批准运行。','workflow','系统自动跟进'),
    result('auto-preview-result',106,'auto-preview','pr_ready_prepared','审核预览已准备好，请核对后单独确认。','workflow','系统自动跟进')];
  let revision=1;
  const view=()=>({...source,state:'draft_pr',busy:false,publisher_mode:'gh',jobs:[],events,
    conversation:{...source.conversation,paused:false,messages:[],results,pending_count:0,
      pending_action:{kind:'ready',source_id:'auto-preview',requires_confirmation:true},
      user_state:{phase:'draft_pr',label:'草稿 PR 已创建',next_step:'请查看审核预览并单独确认。',busy:false}},
    pr_lifecycle:{snapshot,preview,completed:null,unresolved:false},pr_update:{},cla:{},
    execution:{...source.execution,actions:{},publisher_account_hash:account,event_cursor:106,revision:String(revision)}});
  await page.route(url,route=>route.fulfill({json:view()}));
  await page.route(url+'/timeline?*',route=>route.fulfill({json:{events:[],cursor:106,execution:view().execution}}));
  await page.reload();
  const manual=page.locator('.conversation-agent-message').filter({hasText:'此前维护操作准备过审核预览。'});
  await manual.getByText('贡献助手 · 维护操作触发',{exact:true}).waitFor();
  const automatic=page.locator('.conversation-agent-message').filter({hasText:'远端自动检查等待维护者批准运行。'});
  await automatic.getByText('贡献助手 · 系统自动跟进',{exact:true}).waitFor();
  const card=page.locator('.conversation-agent-message').filter({has:page.getByRole('button',{name:'查看审核预览',exact:true})}).last();
  assert.equal(await card.locator('.conversation-message-role').textContent(),'贡献助手 · 系统自动跟进');
  await card.getByRole('button',{name:'查看审核预览',exact:true}).click();
  await page.getByRole('dialog').getByRole('button',{name:'提交正式审核',exact:true}).waitFor();
  assert.deepEqual(writes,[]);
  await page.keyboard.press('Escape');
  // A projection correction updates the visible label without changing text.
  results[0].origin={kind:'request',label:'手动请求触发'};revision++;
  await manual.getByText('贡献助手 · 手动请求触发',{exact:true}).waitFor();
  assert.equal(await manual.locator('.conversation-message-body').textContent(),'此前维护操作准备过审核预览。');
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:1000});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:`/tmp/contribos-post-publication-${width}.png`});
  }
  assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);
  console.log(JSON.stringify({ok:true,sourceLabels:true,previewAvailable:true,separateConsent:true,desktop:true,mobile:true,mutations:0}));
}finally{await browser.close();}
