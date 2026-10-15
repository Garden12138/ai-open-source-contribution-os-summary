// Offline browser acceptance: actual polling, isolated title/body consent and receipts.
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page=await browser.newPage();const errors=[],mutations=[];
page.on('pageerror',e=>errors.push(e.message));
try {
  await page.goto('http://127.0.0.1:8765');
  await page.locator('#sidebar-tasks a').first().click();await page.locator('.conversation-plan').waitFor();
  const task=(await page.evaluate(()=>location.hash)).split('/').at(-1);
  const url=`http://127.0.0.1:8765/api/v1/tasks/${task}/workbench`;
  const source=await (await page.request.get(url)).json();
  const account='b'.repeat(64),pr='https://github.com/fixture/repo/pull/7';
  let jobs=[],events=[],count=0,revision=0;
  const update={preview:null,confirmed:null,completed:null,observed:null,unresolved:false};
  const view=()=>({...source,state:'draft_pr',planner_available:false,pr_update_available:true,publisher_mode:'gh',pr_update:update,cla:{},
    busy:jobs.some(j=>['queued','running'].includes(j.state)),jobs,
    events:[...source.events,{id:'published',sequence:100,kind:'publication_completed',payload:{html_url:pr}},...events],
    execution:{...source.execution,actions:{},publisher_account_hash:account,event_cursor:100+events.length,revision:JSON.stringify({jobs,events,revision})}});
  await page.route(url,route=>route.fulfill({json:view()}));
  await page.route(url+'/timeline?*',route=>route.fulfill({json:{events,cursor:view().execution.event_cursor,execution:view().execution}}));
  const event=(kind,payload)=>{events.push({id:`update-${++revision}`,sequence:101+events.length,kind,payload,job_id:jobs.at(-1)?.id});};
  await page.route(url+'/messages',route=>{
    const body=route.request().postDataJSON();mutations.push({path:'/messages',body});
    jobs.push({id:'read-'+(++count),kind:'pr_update_read',state:'queued'});event('pr_update_requested',{text:body.text});
    return route.fulfill({status:202,json:jobs.at(-1)});
  });
  await page.route(url+'/pr-update/confirm',route=>{
    const body=route.request().postDataJSON();mutations.push({path:'/confirm',body});
    assert.equal(body.preview_hash,update.preview.record_hash);assert.equal(body.nonce,update.preview.nonce);
    assert.equal(body.text,update.preview.acknowledgement);assert.equal(body.approved,true);
    update.preview.consumed=true;update.confirmed={preview_hash:body.preview_hash};
    jobs.push({id:'write-'+count,kind:'pr_update_write',state:'queued'});event('pr_update_confirmed',{text:body.text});
    return route.fulfill({status:202,json:jobs.at(-1)});
  });
  await page.route(url+'/pr-update/check',route=>{
    mutations.push({path:'/check',body:route.request().postDataJSON()});
    jobs.push({id:'check-'+count,kind:'pr_update_check',state:'queued'});return route.fulfill({status:202,json:jobs.at(-1)});
  });
  const preview=()=>{
    jobs.at(-1).state='succeeded';
    update.preview={record_hash:String(count).repeat(64),publisher_account_hash:account,login:'contributor',target:{pr_url:pr,head_sha:'c'.repeat(40)},
      before:{title:'旧中文标题',body:'旧中文正文'},title:'fix(openai): accumulate streamed content',body:'Fixes #3785\n\n## Summary\nPreserve streamed output fragments.\n\n## Testing\nLocal sandbox: 533 passed, 27 skipped.\n\n<script>window.injected=true</script>',
      nonce:'d'.repeat(64),expires_at:'2099-01-01T00:00:00Z',requirements:{sources:[{path:'CONTRIBUTING',commit:'c'.repeat(40)}]},notes:['GitHub CI has not been verified.'],
      acknowledgement:`确认将 ${pr} 的标题和正文更新为上述预览，仅更新 PR 文案。`,consumed:false};
    event('pr_update_prepared',update.preview);
  };
  await page.reload();
  const section=page.getByRole('region',{name:'PR 文案更新',exact:true});await section.waitFor();
  const purpose=page.getByRole('combobox',{name:'消息用途',exact:true});assert.equal(await purpose.isEnabled(),true);
  const input=page.getByRole('textbox',{name:'发送给贡献助手的消息',exact:true});
  await input.fill('请更新 PR #4025 的标题和正文为英文，先给我预览。');await page.getByRole('button',{name:'发送',exact:true}).click();
  assert.equal(mutations.at(-1).body.mode,'question'); // The server recognizes natural requests.
  await section.getByText('正在读取当前 PR、仓库规范并生成英文预览…',{exact:true}).waitFor();
  jobs.at(-1).state='succeeded';jobs.push({id:'document-'+count,kind:'pr_update_document',state:'running'});revision++;
  await page.waitForTimeout(1200);preview();
  await section.getByRole('heading',{name:'更新后的英文标题和正文',exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>!!window.injected),false);
  assert.equal(await section.locator('details').first().getAttribute('open'),null);
  assert.equal(mutations.filter(m=>m.path==='/confirm').length,0);
  const fill=section.getByRole('button',{name:'填写 PR 更新确认到对话',exact:true});
  await fill.click();assert.equal(mutations.filter(m=>m.path==='/confirm').length,0);
  await section.getByRole('checkbox').check();await fill.click();assert.equal(await purpose.inputValue(),'pr_update_confirm');
  const confirm=page.getByRole('button',{name:'确认更新 PR 文案',exact:true});assert.equal(await confirm.isEnabled(),true);
  await confirm.click();await section.getByText('Publisher 正在更新或核对 PR 文案…',{exact:true}).waitFor();
  jobs.at(-1).state='failed';jobs.at(-1).error_message='更新结果尚未明确';update.unresolved=true;revision++;
  await section.getByText(jobs.at(-1).error_message,{exact:true}).waitFor();
  assert.equal(await section.getByRole('button',{name:'在对话中继续修改文案',exact:true}).isDisabled(),true);
  await section.getByRole('button',{name:'核对 PR 更新结果',exact:true}).click();
  jobs.at(-1).state='succeeded';update.unresolved=false;update.completed={preview_hash:update.preview.record_hash,pr_url:pr,title:update.preview.title};
  update.observed={matched:true};event('pr_update_completed',update.completed);event('pr_update_observed',update.observed);
  await page.getByText(`PR 标题和正文已更新并核对：${pr}\n${update.preview.title}`,{exact:true}).waitFor();
  await page.reload();await section.getByText('本次 PR 文案已更新并核对。',{exact:true}).waitFor();
  await section.getByRole('button',{name:'在对话中继续修改文案',exact:true}).click();
  assert.equal(await purpose.inputValue(),'pr_update');await input.fill('请润色 PR 正文');
  await page.getByRole('button',{name:'生成更新预览',exact:true}).click();preview();
  await section.getByRole('checkbox').waitFor();assert.equal(await section.getByRole('checkbox').isChecked(),false);
  update.preview.expires_at='2000-01-01T00:00:00Z';revision++;
  await section.getByText('更新预览已过期或账号已变化，请在对话中重新生成。',{exact:true}).waitFor();
  assert.equal(await section.getByRole('checkbox').count(),0);
  for(const width of [1440,390]) {await page.setViewportSize({width,height:1000});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);}
  assert.deepEqual(errors,[]);assert.equal(mutations.filter(m=>m.path==='/confirm').length,1);
  console.log('PASS: natural conversation request, queued read/model preview, English text/old-new comparison, escaped untrusted text, exact separate consent, unknown result/read-only reconcile, durable receipt, new preview/reset/expiry, desktop/mobile');
} finally {await browser.close();}
