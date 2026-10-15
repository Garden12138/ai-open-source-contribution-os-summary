import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page=await browser.newPage();const errors=[],requests=[];page.on('pageerror',e=>errors.push(e.message));
try {
  await page.goto('http://127.0.0.1:8765');await page.locator('#sidebar-tasks a').first().click();await page.locator('.conversation-plan').waitFor();
  const task=(await page.evaluate(()=>location.hash)).split('/').at(-1),url=`http://127.0.0.1:8765/api/v1/tasks/${task}/workbench`;
  const source=await (await page.request.get(url)).json(),account='b'.repeat(64),prUrl='https://github.com/fixture/repo/pull/7';
  let jobs=[],events=[],revision=0;
  const lifecycle={snapshot:null,preview:null,completed:null,unresolved:false};
  const before={node_id:'PR_fixture123456',title:'fix(openai): accumulate streamed content',body:'Fixes #3785\n\n## Summary\nPreserve streamed content.',head_sha:'c'.repeat(40),base_sha:'d'.repeat(40),draft:true,state:'open',merged:false};
  const view=()=>({...source,state:'draft_pr',busy:jobs.some(j=>['queued','running'].includes(j.state)),planner_available:false,pr_update_available:true,publisher_mode:'gh',cla:{},pr_update:{},pr_lifecycle:lifecycle,jobs,
    events:[...source.events,{id:'published',sequence:100,kind:'publication_completed',payload:{html_url:prUrl}},...events],
    execution:{...source.execution,actions:{},publisher_account_hash:account,pr_lifecycle:lifecycle,event_cursor:100+events.length,revision:JSON.stringify({jobs,events,revision})}});
  const event=(kind,payload)=>events.push({id:'lifecycle-'+(++revision),sequence:101+events.length,kind,job_id:jobs.at(-1)?.id,payload});
  await page.route(url,route=>route.fulfill({json:view()}));
  await page.route(url+'/timeline?*',route=>route.fulfill({json:{events,cursor:view().execution.event_cursor,execution:view().execution}}));
  await page.route(url+'/messages',route=>{
    const body=route.request().postDataJSON();requests.push({path:'/messages',body});
    jobs.push({id:'ready-prepare',kind:'pr_ready_prepare',state:'queued'});event('pr_lifecycle_requested',{text:body.text});
    return route.fulfill({status:202,json:jobs.at(-1)});
  });
  await page.route(url+'/pr-ready/confirm',route=>{
    const body=route.request().postDataJSON();requests.push({path:'/confirm',body});
    assert.equal(body.preview_hash,lifecycle.preview.record_hash);assert.equal(body.nonce,lifecycle.preview.nonce);
    assert.equal(body.text,lifecycle.preview.acknowledgement);assert.equal(body.approved,true);
    lifecycle.preview.consumed=true;jobs.push({id:'ready-write',kind:'pr_ready_write',state:'queued'});event('pr_ready_confirmed',{text:body.text});
    return route.fulfill({status:202,json:jobs.at(-1)});
  });
  await page.route(url+'/pr-followup',route=>{
    requests.push({path:'/followup',body:route.request().postDataJSON()});jobs.push({id:'followup-'+revision,kind:'pr_followup',state:'queued'});
    return route.fulfill({status:202,json:jobs.at(-1)});
  });
  function observe(stage,ci,nextAction,overrides={}) {
    jobs.at(-1).state='succeeded';
    lifecycle.snapshot={stage,ci,next_actor:'maintainer',next_action:nextAction,observed_at:'2026-10-12T09:00:00Z',
      pr:{...before,draft:stage==='draft',merged:stage==='merged',state:stage==='merged' || stage==='closed' ? 'closed' : 'open'},target:{pr_url:prUrl},checks:[],workflows:[],statuses:[],reviews:[],comments:[],warnings:[],...overrides};
    event('pr_followup_observed',lifecycle.snapshot);
  }
  await page.reload();const section=page.getByRole('region',{name:'PR 审核与发布后跟进',exact:true});await section.waitFor();
  const purpose=page.getByRole('combobox',{name:'消息用途',exact:true}),input=page.getByRole('textbox',{name:'发送给贡献助手的消息',exact:true});
  await input.fill('请准备将当前 PR 转为 Ready for review，先给我确认预览。');await page.getByRole('button',{name:'发送',exact:true}).click();
  assert.equal(requests.at(-1).body.mode,'question');await section.getByText('正在读取 GitHub PR、CI 和审核反馈…',{exact:true}).waitFor();
  observe('draft','awaiting_approval','代码与文案准备好后，可在对话中请求转为 Ready for review。');
  lifecycle.preview={record_hash:'a'.repeat(64),target:{pr_url:prUrl},before,login:'contributor',publisher_account_hash:account,nonce:'e'.repeat(64),expires_at:'2099-01-01T00:00:00Z',consumed:false,
    acknowledgement:`确认将 ${prUrl} 转为 Ready for review，提交 ${before.head_sha}；此操作不代表 CI 或审核已通过。`};event('pr_ready_prepared',lifecycle.preview);
  const fill=section.getByRole('button',{name:'填写正式审核确认到对话',exact:true});await fill.waitFor();
  assert.equal(await section.getByRole('checkbox').isChecked(),false);await fill.click();assert.equal(requests.filter(x=>x.path==='/confirm').length,0);
  await section.getByRole('checkbox').check();await fill.click();assert.equal(await purpose.inputValue(),'pr_ready_confirm');
  const submit=page.getByRole('button',{name:'确认转为正式审核',exact:true});assert.equal(await submit.isEnabled(),true);await submit.click();
  await section.getByText('正在转为正式审核或核对结果…',{exact:true}).waitFor();
  jobs.at(-1).state='failed';jobs.at(-1).error_message='审核状态结果尚未明确';lifecycle.unresolved=true;revision++;
  await section.getByText(jobs.at(-1).error_message,{exact:true}).waitFor();assert.equal(await section.getByRole('button',{name:'准备转为正式审核',exact:true}).count(),0);
  const follow=section.getByRole('button',{name:'刷新 PR 跟进',exact:true});await follow.click();
  observe('ci_awaiting_approval','awaiting_approval','Fork 工作流等待上游维护者批准运行；等待批准不等于测试失败。');
  lifecycle.unresolved=false;lifecycle.completed={preview_hash:lifecycle.preview.record_hash,pr_url:prUrl,head_sha:before.head_sha};event('pr_ready_completed',lifecycle.completed);
  await page.getByRole('heading',{name:'PR 已提交正式审核',exact:true}).waitFor();
  await section.getByText('CI 等待维护者批准',{exact:true}).waitFor();assert.equal(await section.getByRole('checkbox').count(),0);
  await page.reload();await section.getByText('CI 等待维护者批准',{exact:true}).waitFor();
  assert.equal(await purpose.locator('option[value="pr_update"]').getAttribute('disabled'),null);
  await follow.click();observe('changes_requested','failed','阅读审核意见，修订代码后在沙箱验证。',{
    reviews:[{login:'maintainer',state:'CHANGES_REQUESTED',body:'Please add an empty stream regression.',url:prUrl+'#pullrequestreview-31'}],
    comments:[{login:'reviewer',body:'<script>window.injected=true</script>',path:'src/stream.py',line:42,url:'https://evil.example/steal'}],
    failures:[{name:'Python tests',conclusion:'failure',steps:[{name:'Run pytest',conclusion:'failure'}]}],
    annotations:[{path:'src/stream.py',line:42,message:'Expected all fragments',level:'failure'}]});
  await section.getByText('审核要求修改',{exact:true}).waitFor();await section.getByText('检查、审核意见与讨论记录',{exact:true}).click();
  await section.getByText('Please add an empty stream regression.',{exact:true}).waitFor();
  assert.equal(await section.getByText('失败步骤：Run pytest · failure',{exact:true}).count(),1);
  assert.equal(await page.evaluate(()=>!!window.injected),false);assert.equal(await page.locator('a[href="https://evil.example/steal"]').count(),0);
  await follow.click();observe('merged','observed_passed','PR 已合并，保留贡献记录。');await page.getByRole('heading',{name:'PR 已合并',exact:true}).waitFor();
  lifecycle.preview={...lifecycle.preview,record_hash:'f'.repeat(64),consumed:false};
  lifecycle.snapshot.pr={...before};lifecycle.snapshot.stage='draft';revision++;
  await section.getByRole('checkbox').waitFor();assert.equal(await section.getByRole('checkbox').isChecked(),false);
  await section.getByRole('checkbox').check();lifecycle.preview.expires_at='2000-01-01T00:00:00Z';revision++;
  await section.getByText('审核状态预览已过期、账号或操作状态已变化，请重新准备。',{exact:true}).waitFor();
  assert.equal(await section.getByRole('checkbox').count(),0);
  lifecycle.preview.expires_at='2099-01-01T00:00:00Z';lifecycle.preview.publisher_account_hash='9'.repeat(64);revision++;
  await page.waitForTimeout(1500);assert.equal(await section.getByRole('checkbox').count(),0);
  for (const width of [1440,390]) {await page.setViewportSize({width,height:1000});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);}
  assert.deepEqual(errors,[]);assert.equal(requests.filter(x=>x.path==='/confirm').length,1);
  console.log('PASS: model-free natural ready preview, unchecked exact separate consent, unknown/read-only follow-up, durable ready state, metadata remains available, CI approval/changes/failure steps/merged, escaped feedback/source links, desktop/mobile');
} finally {await browser.close();}
