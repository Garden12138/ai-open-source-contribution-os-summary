// Offline native UI contracts. Protected mutations are intercepted; this is
// browser regression evidence, not real model/sandbox/GitHub acceptance.
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
import {executionDetail, diff, inventory} from './execution_changes_fixtures.mjs';
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page = await browser.newPage({viewport:{width:1440,height:960}});
const errors=[], requests=[];
page.on('pageerror', e => errors.push(e.message));
try {
  await page.goto('http://127.0.0.1:8765');
  await page.locator('#sidebar-tasks a').first().click();
  await page.locator('.conversation-agent-message').first().waitFor();
  const task = (await page.evaluate(() => location.hash)).split('/').at(-1);
  const url = `http://127.0.0.1:8765/api/v1/tasks/${task}/workbench`;
  const source = await (await page.request.get(url)).json(), account='b'.repeat(64);
  let sequence=200, reject=false, state='planning', busy=false;
  let pending=0, paused=false, events=[], messages=[], results=[], previews={}, freshTask=false;
  const projection={...source.execution, actions:{can_execute:true}, publisher_account_hash:account};
  const snapshot=()=>({...source,state,busy,publisher_mode:'gh',planner_available:true,
    events:freshTask?[]:[...source.events,...events], plans:freshTask?[]:source.plans, jobs:freshTask?[]:source.jobs,
    execution:projection,cla:previews.cla || {},pr_update:previews.update || {},pr_lifecycle:previews.ready || {},
    conversation:{version:'conversation-v1',messages,results,pending_count:pending,paused,
      pending_action:!busy && !pending && !paused && events.at(-1)?.payload?.questions?.length
        ? {kind:'answer',source_id:events.at(-1).id} : null,
      user_state:{label:busy?'正在实现与验证':'准备贡献计划',next_step:'说明要求即可继续。',busy,can_stop:busy,observed_at:null}}});
  await page.route(url, route => route.fulfill({json:snapshot()}));
  await page.route(url+'/conversation/messages', route => {
    const body=route.request().postDataJSON(), key=route.request().headers()['idempotency-key'];
    requests.push({path:'message',body,key});
    if (reject) { reject=false; return route.fulfill({status:409,json:{detail:'消息暂未保存，请重新发送。'}}); }
    const id='message-'+(++sequence);
    messages.push({id,sequence,text:body.text,receipt:{message_id:id,status:'queued'}});
    pending++; return route.fulfill({status:202,json:{message_id:id,status:'queued',text:'已收到，会按顺序处理。'}});
  });
  await page.route(url+'/execute', route => {
    requests.push({path:'execute',body:route.request().postDataJSON()});
    busy=true;state='executing';projection.actions={};
    return route.fulfill({status:202,json:{id:'execution-job',state:'queued'}});
  });
  await page.route(url+'/acceptance', route => {
    requests.push({path:'acceptance',body:route.request().postDataJSON()});
    projection.actions={};projection.acceptance.accepted={record_hash:'c'.repeat(64)};
    return route.fulfill({json:{accepted:true,binding_hash:projection.acceptance.binding_hash,record_hash:'c'.repeat(64)}});
  });
  for (const [kind,path] of [['publication','publication/confirm'],['ready','pr-ready/confirm'],['update','pr-update/confirm'],['cla','cla/confirm']]) {
    await page.route(url+'/'+path, route => {
      requests.push({path:kind,body:route.request().postDataJSON()});
      if(kind==='publication') events.push({id:'confirmed',sequence:++sequence,kind:'publication_confirmed',payload:{}});
      else previews[kind].preview.consumed=true;
      return route.fulfill({status:202,json:{id:kind+'-job',state:'queued'}});
    });
  }
  await page.route(url+'/publication/renew', route => {
    requests.push({path:'renew',body:route.request().postDataJSON()});
    busy=true;
    return route.fulfill({status:202,json:{id:'renew-preview',kind:'publication_prepare',state:'queued'}});
  });
  await page.reload();
  const input=page.getByRole('textbox',{name:'发送给贡献助手的消息',exact:true});
  const send=page.getByRole('button',{name:'发送',exact:true});
  const main=page.locator('.conversation-agent-messages');
  assert.equal(await page.locator('.conversation-agent select').count(),0);
  assert.equal(await main.locator('details').count(),0);
  freshTask=true;await page.reload();
  assert.equal(requests.length,0);
  await page.getByRole('button',{name:'读取仓库并准备方案',exact:true}).click();
  assert.deepEqual(requests.at(-1).body,{text:'读取仓库并准备方案'});
  assert.equal(requests.at(-1).path,'message');
  await page.getByRole('button',{name:'读取仓库并准备方案',exact:true}).waitFor({state:'hidden'});
  freshTask=false;messages=[];pending=0;requests.length=0;await page.reload();
  await input.fill('尚未发送的草稿');await page.reload();assert.equal(await input.inputValue(),'尚未发送的草稿');
  await input.dispatchEvent('compositionstart');
  await input.dispatchEvent('keydown',{key:'Enter',code:'Enter',isComposing:true});
  assert.equal(requests.length,0);
  await input.dispatchEvent('compositionend');
  await input.press('Shift+Enter');assert.equal(requests.length,0);assert.match(await input.inputValue(),/\n/);
  reject=true;await send.click();
  await page.getByRole('alert').filter({hasText:'消息暂未保存'}).waitFor();
  assert.equal(await input.inputValue(),'尚未发送的草稿\n');
  await input.press('Enter');
  await main.getByText('尚未发送的草稿',{exact:true}).waitFor();
  assert.equal(requests[0].key,requests[1].key);assert.deepEqual(requests[1].body,{text:'尚未发送的草稿'});
  assert.equal(await input.inputValue(),'');
  assert.equal(await main.getByText('已收到，等待处理',{exact:true}).count(),1);
  messages[0].receipt.status='completed';pending=0;
  await page.reload();
  await page.getByRole('button',{name:'查看方案',exact:true}).click();
  const drawer=page.getByRole('dialog');await drawer.waitFor();
  assert.equal(await drawer.getByRole('heading',{name:'修改范围',exact:true}).count(),1);
  await page.keyboard.press('Escape');assert.equal(await drawer.isVisible(),false);
  assert.equal(await page.getByRole('button',{name:'查看方案',exact:true}).evaluate(el=>el===document.activeElement),true);
  projection.actions={can_execute:false};
  projection.readiness={blockers:[{code:'environment_unsupported',message:'动态依赖尚未适配；当前方案不能启动实施。'}]};
  await page.reload();
  await main.getByText('动态依赖尚未适配；当前方案不能启动实施。',{exact:true}).waitFor();
  assert.equal(await page.getByRole('button',{name:'按此方案开始',exact:true}).count(),0);
  projection.actions={can_execute:true};projection.readiness={blockers:[]};await page.reload();
  await page.getByRole('button',{name:'按此方案开始',exact:true}).waitFor();
  await page.getByRole('button',{name:'按此方案开始',exact:true}).click();
  assert.equal(requests.at(-1).path,'execute');assert.equal(requests.at(-1).body.plan_hash,source.plans.at(-1).record_hash);
  await page.getByRole('button',{name:'停止',exact:true}).waitFor();
  await input.fill('还要考虑空字符串');await send.click();
  await input.fill('只改这个包');await send.click();
  assert.deepEqual(requests.slice(-2).map(r=>r.body.text),['还要考虑空字符串','只改这个包']);
  assert.equal(await input.isEnabled(),true);
  await page.getByRole('button',{name:'停止',exact:true}).click();assert.equal(requests.at(-1).body.text,'停止');
  busy=false;state='ready';pending=0;messages.forEach(m=>m.receipt.status='completed');
  const detail=executionDetail();
  projection.actions={can_accept:true};projection.acceptance={available:true,binding_hash:'d'.repeat(64),accepted:null};
  projection.latest_attempt_id=detail.id;projection.verification={status:'passed',passed:5,total:5};
  projection.attempts=[{id:detail.id,number:1,verification:projection.verification}];
  await page.route(`**/api/v1/executions/${detail.id}`, route=>route.fulfill({json:detail}));
  await page.route(`**/api/v1/executions/${detail.id}/artifacts/*`, route=>route.fulfill(
    route.request().url().endsWith('a'.repeat(64)) ? {contentType:'text/x-diff',body:diff} : {json:inventory}));
  await page.reload();
  await page.getByRole('button',{name:'查看改动与测试',exact:true}).click();
  await drawer.getByText('5/5 条验证命令通过。',{exact:true}).waitFor();
  await drawer.locator('details.changes-file').first().locator('summary').click();
  assert.equal(await drawer.locator('img').count(),0);
  await drawer.getByRole('button',{name:'返回对话',exact:true}).click();
  await page.getByRole('button',{name:'确认结果',exact:true}).click();
  assert.equal(requests.at(-1).body.prepare_pr,true);
  assert.equal(requests.at(-1).body.binding_hash,'d'.repeat(64));
  const before={title:'fix: preserve streaming',body:'## Summary\nPreserve content.',head_sha:'c'.repeat(40),base_sha:'d'.repeat(40)};
  const p={record_hash:'e'.repeat(64),publisher_account_hash:account,nonce:'f'.repeat(64),expires_at:'2099-01-01T00:00:00Z',
    title:before.title,body:before.body,login:'contributor',target:{pr_url:'https://github.com/fixture/repo/pull/7'},before,
    acknowledgement:'fixed exact acknowledgement',consumed:false};
  for (const kind of ['publication','update','ready','cla']) {
    events=[];previews={};
    if(kind==='publication') events.push({id:'pub-preview',sequence:++sequence,kind:'publication_prepared',record_hash:p.record_hash,
      payload:{...p,repository:'fixture/repo',base_branch:'main',fork:'contributor/repo'}});
    else {
      events.push({id:'published',sequence:++sequence,kind:'publication_completed',payload:{}});
      previews[kind]={preview:{...p,record_hash:({update:'1',ready:'2',cla:'3'})[kind].repeat(64),
        agreement_hash:'4'.repeat(64),agreement_text:'# Full agreement\n<script>window.injected=true</script>',comment:'I hereby sign the CLA',status:'failed'}};
      events.push({id:kind+'-preview',sequence:++sequence,kind:({update:'pr_update_prepared',ready:'pr_ready_prepared',cla:'cla_prepared'})[kind],payload:previews[kind].preview});
    }
    await page.reload();await page.getByRole('button',{name:kind==='ready'?'查看审核预览':'查看完整预览',exact:true}).click();
    assert.equal(await drawer.locator('select').count(),0);
    const action=drawer.getByRole('button',{name:({publication:'创建草稿 PR',update:'确认更新文案',ready:'提交正式审核',cla:'同意并确认签署'})[kind],exact:true});
    if(kind==='cla') {
      assert.equal(await action.isDisabled(),true);assert.equal(await drawer.getByRole('checkbox').isChecked(),false);
      assert.equal(await page.evaluate(()=>!!window.injected),false);
      await drawer.getByRole('checkbox').check();
    } else assert.equal(await drawer.getByRole('checkbox').count(),0);
    assert.equal(await action.isEnabled(),true);
    await action.click();await page.waitForFunction(()=>!document.querySelector('.conversation-agent-drawer').open);
    assert.equal(requests.at(-1).path,kind);
    if(kind==='publication') assert.deepEqual(requests.at(-1).body,{intent_hash:p.record_hash,nonce:p.nonce});
    else assert.equal(requests.at(-1).body.preview_hash,previews[kind].preview.record_hash);
  }
  // A preview expiring while open gains a direct recovery control. Renewal is
  // separate from publication, preserves text, and requires a new exact review.
  events=[{id:'expiring',sequence:++sequence,kind:'publication_prepared',record_hash:'6'.repeat(64),
    payload:{...p,expires_at:new Date(Date.now()+1000).toISOString()}}];previews={};
  await page.reload();await page.getByRole('button',{name:'查看完整预览',exact:true}).click();
  await page.waitForTimeout(2500);
  assert.equal(await drawer.getByRole('button',{name:'创建草稿 PR',exact:true}).isDisabled(),true);
  await drawer.getByText('预览已过期。请沿用当前标题和正文更新预览，重新核对后再创建草稿 PR。',{exact:true}).waitFor();
  const renew=drawer.getByRole('button',{name:'沿用文案更新预览',exact:true});
  assert.equal(await renew.isEnabled(),true);
  const beforeRenew=requests.length;
  await renew.click();await page.waitForFunction(()=>!document.querySelector('.conversation-agent-drawer').open);
  assert.deepEqual(requests.slice(beforeRenew),[{path:'renew',body:{preview_hash:'6'.repeat(64)}}]);
  await page.getByRole('button',{name:'查看完整预览',exact:true}).click();
  assert.equal(await drawer.getByRole('button',{name:'沿用文案更新预览',exact:true}).isDisabled(),true);
  await drawer.getByRole('button',{name:'返回对话',exact:true}).click();
  busy=false;events=[{...events[0],id:'renewed',sequence:++sequence,record_hash:'7'.repeat(64),payload:{...p,nonce:'9'.repeat(64)}}];
  await page.reload();await page.getByRole('button',{name:'查看完整预览',exact:true}).click();
  assert.equal(await drawer.getByRole('button',{name:'创建草稿 PR',exact:true}).isEnabled(),true);
  assert.equal(await drawer.getByRole('button',{name:'沿用文案更新预览',exact:true}).isVisible(),false);
  assert.equal(requests.length,beforeRenew+1);
  await drawer.getByRole('button',{name:'返回对话',exact:true}).click();
  // Old preview expires while open; queued changes also disable confirmation.
  events=[];previews={ready:{preview:{...p,expires_at:new Date(Date.now()+1000).toISOString()}}};
  await page.reload();await page.getByRole('button',{name:'查看审核预览',exact:true}).click();
  await page.waitForTimeout(2500);
  assert.equal(await drawer.getByRole('button',{name:'提交正式审核',exact:true}).isDisabled(),true);
  await drawer.getByRole('button',{name:'返回对话',exact:true}).click();
  // Reading old messages does not jump down when a result arrives.
  previews={};results=Array.from({length:40},(_,i)=>({id:'history-'+i,sequence:1000+i,text:'已完成一项检查。\n'.repeat(4)}));
  await page.reload();await main.locator('article').last().waitFor();
  await main.evaluate(el=>{el.scrollTop=0;});
  results.push({id:'new-result',sequence:2000,text:'新结果已准备好。'});
  await page.getByRole('button',{name:'有新消息 ↓',exact:true}).waitFor();
  assert.equal(await main.evaluate(el=>el.scrollTop),0);
  await page.getByRole('button',{name:'有新消息 ↓',exact:true}).click();
  for(const width of [1440,390]) {
    await page.setViewportSize({width,height:900});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    assert.equal(await input.isVisible(),true);
    await page.screenshot({path:`/tmp/contribos-conversation-agent-${width}.png`});
    await page.getByRole('button',{name:'历史记录',exact:true}).click();
    assert.equal(await drawer.locator('details details').count(),0);
    if(width===390) assert.equal(Math.round((await drawer.boundingBox()).width),390);
    await drawer.getByRole('button',{name:'返回对话',exact:true}).click();
  }
  await page.setViewportSize({width:390,height:480});await input.focus();
  const composerBox=await page.locator('.conversation-agent-composer').boundingBox();
  assert.ok(composerBox.y+composerBox.height<=480);
  const questions=[{id:'scope',prompt:'只修改当前包吗？',options:['只改当前包','同时修改示例']},
    {id:'tests',prompt:'测试覆盖范围？',options:['单元测试','增加集成测试']},
    {id:'notes',prompt:'还有补充要求吗？',options:[]}];
  events.push({id:'clarification',sequence:3000,record_hash:'a'.repeat(64),kind:'assistant_message',payload:{reply:'',questions}});
  messages=[{id:'clarify-request',sequence:2999,text:'请调整范围',receipt:{status:'completed'}}];
  await page.reload();await main.getByRole('group',{name:'1. 只修改当前包吗？',exact:true}).waitFor();
  const batch=main.locator('.conversation-questions'), answerSend=batch.getByRole('button',{name:'发送回答',exact:true});
  const beforeAnswers=requests.length;
  assert.equal(await answerSend.isDisabled(),true);
  assert.equal(await batch.getByRole('radio',{checked:true}).count(),0);
  await batch.getByRole('radio',{name:'只改当前包',exact:true}).check();
  // Native arrow-key selection and a change of mind do not submit any message.
  await batch.getByRole('radio',{name:'单元测试',exact:true}).focus();
  await page.keyboard.press('ArrowDown');
  assert.equal(await batch.getByRole('radio',{name:'增加集成测试',exact:true}).isChecked(),true);
  await batch.getByRole('radio',{name:'同时修改示例',exact:true}).check();
  const custom=batch.getByRole('textbox',{name:'还有补充要求吗？：自定义回答',exact:true});
  await batch.getByRole('group',{name:'3. 还有补充要求吗？',exact:true}).getByRole('radio',{name:'自定义回答',exact:true}).check();
  await custom.fill('   ');assert.equal(await answerSend.isDisabled(),true);
  await custom.fill('保持现有 API\n<script>window.injected=true</script>');
  assert.equal(await answerSend.isEnabled(),true);assert.equal(requests.length,beforeAnswers);
  await input.fill('另一个尚未发送的草稿');
  await page.waitForTimeout(2300); // Polling must retain selection and focus/typing.
  await page.reload();
  assert.equal(await batch.getByRole('radio',{name:'同时修改示例',exact:true}).isChecked(),true);
  assert.equal(await batch.getByRole('radio',{name:'增加集成测试',exact:true}).isChecked(),true);
  assert.equal(await custom.inputValue(),'保持现有 API\n<script>window.injected=true</script>');
  assert.equal(await input.inputValue(),'另一个尚未发送的草稿');
  await custom.fill('很'.repeat(8000));
  await batch.getByText('回答合并后超过 8000 字，请缩短自定义回答。',{exact:true}).waitFor();
  assert.equal(await answerSend.isDisabled(),true);
  await custom.fill('保持现有 API\n<script>window.injected=true</script>');
  for(const width of [1440,390]) {
    await page.setViewportSize({width,height:900});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:`/tmp/contribos-question-options-${width}.png`});
  }
  reject=true;await answerSend.click();
  await page.getByRole('alert').filter({hasText:'消息暂未保存'}).waitFor();
  assert.equal(await answerSend.isEnabled(),true);
  const failedAnswer=requests.at(-1);
  await page.reload();await answerSend.click();
  await main.getByText('已收到，等待处理',{exact:true}).waitFor();
  assert.equal(requests.at(-1).key,failedAnswer.key);
  assert.deepEqual(requests.at(-1).body,{text:'回答你的问题：\n\n1. 只修改当前包吗？\n我的回答：同时修改示例\n\n2. 测试覆盖范围？\n我的回答：增加集成测试\n\n3. 还有补充要求吗？\n我的回答：保持现有 API\n<script>window.injected=true</script>',
    question_source_id:'clarification',question_source_hash:'a'.repeat(64)});
  assert.equal(messages.filter(m=>m.text.startsWith('回答你的问题：')).length,1);
  assert.equal(requests.length,beforeAnswers+2); // Only the failed request and its idempotent retry.
  assert.equal(await main.locator('.conversation-questions').count(),0);
  assert.equal(await input.inputValue(),'另一个尚未发送的草稿');
  assert.equal(await page.evaluate(()=>!!window.injected),false);
  // Even after processing finishes, old questions must never become actionable.
  pending=0;messages.forEach(m=>m.receipt.status='completed');await page.reload();
  assert.equal(await main.getByRole('radio').count(),0);
  // A new batch may reuse question IDs; choices belong to its source message.
  events.push({id:'clarification-next',sequence:3100,record_hash:'b'.repeat(64),kind:'assistant_message',payload:{reply:'',questions}});
  await page.reload();assert.equal(await batch.getByRole('radio',{checked:true}).count(),0);
  paused=true;await page.reload();assert.equal(await main.getByRole('radio').count(),0);
  paused=false;busy=true;await page.reload();assert.equal(await main.getByRole('radio').count(),0);
  busy=false;
  // A follow-up answer cites the exact existing preview and shows its full text
  // inline. Reading it cannot request publication or silently replace a source.
  const cited={id:'cited-preview',sequence:4000,kind:'publication_prepared',record_hash:'8'.repeat(64),payload:{...p,
    title:'Fix directory docstring typo',body:'## Summary\nCorrect will crete to will create.\n\n## Testing\n8 passed.',repository:'fixture/repo',fork:'contributor/repo'}};
  events=[cited];previews={};results=[{id:'agent-answer',sequence:4002,text:'这是你刚才要看的已有预览。',source_kind:'agent_reply',
    references:[{name:'publication_preview',source_id:cited.id,source_hash:cited.record_hash}]}];
  const previousWrites=requests.length;
  for(const width of [1440,390]) {
    await page.setViewportSize({width,height:900});await page.reload();
    const inline=main.locator('details').filter({has:page.getByText('发布预览 · 完整标题与正文',{exact:true})});
    await inline.waitFor();assert.equal(await inline.getAttribute('open'),'');
    await inline.getByRole('heading',{name:cited.payload.title,exact:true}).waitFor();
    assert.ok((await inline.innerText()).includes('8 passed.'));
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  }
  assert.equal(requests.length,previousWrites);
  // A real ready request can still carry an old creation reference. It must
  // become read-only history while the separate ready control stays current.
  const ready={...p,record_hash:'5'.repeat(64)};
  events=[cited,{id:'published',sequence:4100,kind:'publication_completed',payload:{html_url:'https://github.com/fixture/repo/pull/7',number:7}},
    {id:'ready-current',sequence:4102,kind:'pr_ready_prepared',record_hash:ready.record_hash,payload:ready}];
  previews={ready:{preview:ready}};
  results=[{id:'ready-request-answer',sequence:4101,text:'我会准备提交正式审核的预览。',
    references:[{name:'publication_preview',source_id:cited.id,source_hash:cited.record_hash}]}];
  for(const width of [1440,390]) {
    await page.setViewportSize({width,height:900});await page.reload();
    const historical=main.getByRole('button',{name:'查看历史发布预览',exact:true});await historical.waitFor();
    await historical.click();
    await drawer.getByRole('heading',{name:'历史发布预览',exact:true}).waitFor();
    assert.equal(await drawer.getByRole('button',{name:'创建草稿 PR',exact:true}).count(),0);
    assert.equal(await drawer.getByRole('button',{name:'沿用文案更新预览',exact:true}).count(),0);
    await drawer.getByRole('button',{name:'返回对话',exact:true}).click();
    await main.getByRole('button',{name:'查看审核预览',exact:true}).click();
    await drawer.getByRole('heading',{name:'提交正式审核',exact:true}).waitFor();
    assert.equal(await drawer.getByRole('button',{name:'提交正式审核',exact:true}).isEnabled(),true);
    assert.equal(await drawer.getByRole('button',{name:'创建草稿 PR',exact:true}).count(),0);
    await drawer.getByRole('button',{name:'返回对话',exact:true}).click();
  }
  assert.equal(requests.length,previousWrites);
  results.push({id:'ready-evidence-answer',sequence:4103,text:'这是当前已经准备好的审核预览。',
    references:[{name:'ready_preview',source_id:'ready-current',source_hash:ready.record_hash}]});
  await page.reload();
  const readyAnswer=main.locator('article').filter({hasText:'这是当前已经准备好的审核预览。'});
  await readyAnswer.getByRole('button',{name:'查看审核预览',exact:true}).click();
  await drawer.getByRole('heading',{name:'提交正式审核',exact:true}).waitFor();
  assert.equal(await drawer.getByRole('button',{name:'创建草稿 PR',exact:true}).count(),0);
  await drawer.getByRole('button',{name:'返回对话',exact:true}).click();
  assert.equal(requests.length,previousWrites);
  // Publication receipts expose the exact server-projected PR URL as a link.
  events=[{id:'published-link',sequence:5000,kind:'publication_completed',payload:{html_url:'https://github.com/fixture/repo/pull/7',number:7}}];
  results=[{id:'published-result',sequence:5001,source_kind:'publication_completed',source_id:'published-link',
    text:'草稿 PR 已创建。你可以让我跟进项目检查与维护者反馈。\n\n[查看 PR #7](https://github.com/fixture/repo/pull/7)'}];
  for(const width of [1440,390]) {
    await page.setViewportSize({width,height:900});await page.reload();
    const link=main.getByRole('link',{name:'查看 PR #7',exact:true});await link.waitFor();
    assert.equal(await link.getAttribute('href'),'https://github.com/fixture/repo/pull/7');
    assert.equal(await link.getAttribute('target'),'_blank');
    assert.match(await link.getAttribute('rel'),/noopener/);
  }
  assert.equal(requests.length,previousWrites);
  assert.deepEqual(errors,[]);
  console.log('PASS: unified composer, drafts/error retry, IME/Enter, append/stop, chronological messages, exact contextual confirmations, separate legal consent, expiry, code rendering, focus/scroll, desktop/mobile');
} finally {await browser.close();}
