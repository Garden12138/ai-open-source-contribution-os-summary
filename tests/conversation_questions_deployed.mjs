// Read-only acceptance of existing clarification data. Selections stay in this
// disposable browser; never submit answers or invoke the model for the user.
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser=await chromium.launch({headless:true});
const page=await browser.newPage({viewport:{width:1440,height:960}});
const task=process.env.QUESTION_TASK || 'ac50e857-e176-434e-94ef-677fbc00f4cf';
const origin='http://127.0.0.1:8000', errors=[], mutations=[];
page.on('pageerror',e=>errors.push(e.message));
try {
  await page.route('**/*',route=>{
    if(!['GET','HEAD','OPTIONS'].includes(route.request().method())) {
      mutations.push(route.request().url());return route.abort();
    }
    return route.continue();
  });
  const url=`${origin}/api/v1/tasks/${task}/workbench`;
  const before=await (await page.request.get(url)).json();
  const awaiting=before.conversation.pending_action?.kind==='answer';
  const source=awaiting ? before.events.find(e=>e.id===before.conversation.pending_action.source_id)
    : before.events.filter(e=>e.kind==='assistant_message' && e.payload.questions?.length).at(-1);
  assert.ok(source.payload.questions.length);
  const baseline=x=>JSON.stringify({events:x.events.map(e=>[e.id,e.record_hash]),
    jobs:x.jobs.map(j=>[j.id,j.state,j.attempt_count]),messages:x.conversation.messages});
  await page.goto(`${origin}/#/tasks/${task}`);
  await page.locator('.conversation-agent-message').first().waitFor();
  const batch=page.locator('.conversation-questions');
  if(awaiting) {
    await batch.waitFor();
    const send=batch.getByRole('button',{name:'发送回答',exact:true});
    assert.equal(await send.isDisabled(),true);
    const fields=batch.getByRole('group');
    assert.equal(await fields.count(),source.payload.questions.length);
    for(let i=0;i<source.payload.questions.length;i++) {
      const q=source.payload.questions[i];
      if(q.options.length) await fields.nth(i).getByRole('radio',{name:q.options[0],exact:true}).check();
      else {
        await fields.nth(i).getByRole('radio',{name:'自定义回答',exact:true}).check();
        await fields.nth(i).getByRole('textbox').fill('临时浏览器验证草稿');
      }
      assert.equal(await send.isDisabled(),i<source.payload.questions.length-1);
    }
    await page.reload();await send.waitFor();
    assert.equal(await batch.getByRole('radio',{checked:true}).count(),source.payload.questions.length);
    assert.equal(await send.isEnabled(),true);
  } else {
    assert.ok(before.conversation.messages.some(m=>m.sequence>source.sequence));
    assert.equal(await batch.count(),0);
    if(before.execution?.actions?.can_execute) {
      await page.getByRole('button',{name:'查看方案',exact:true}).last().waitFor();
      assert.equal(await page.getByRole('button',{name:'按此方案开始',exact:true}).isEnabled(),true);
    } else if(before.execution?.preflight?.status==='blocked') {
      assert.equal(await page.getByRole('button',{name:'按此方案开始',exact:true}).count(),0);
      await page.getByRole('button',{name:'查看方案',exact:true}).last().click();
      const drawer=page.getByRole('dialog');await drawer.waitFor();
      assert.ok((await drawer.innerText()).includes('ruff'));
      await drawer.getByRole('button',{name:'返回对话',exact:true}).click();
    }
  }
  if(before.native_resources?.preview?.current && !before.native_resources.ready) {
    await page.getByRole('button',{name:'查看资源下载授权',exact:true}).click();
    const drawer=page.getByRole('dialog');await drawer.waitFor();
    assert.ok((await drawer.innerText()).includes(before.native_resources.preview.resource.sha256));
    assert.ok((await drawer.innerText()).includes(before.native_resources.preview.resource.url));
    await page.waitForFunction(()=>[...document.querySelectorAll('dialog button')].some(b=>b.textContent==='授权下载这些资源' && !b.disabled));
    assert.equal(await drawer.getByRole('button',{name:'授权下载这些资源',exact:true}).isEnabled(),true);
  }
  if(before.native_resources?.ready && before.execution?.actions?.can_execute && before.conversation.pending_action?.kind==='execute') {
    const review=page.getByRole('button',{name:'核对方案并继续',exact:true});
    await review.waitFor();
    assert.equal(await page.locator('.conversation-agent-messages > article').last().getByRole('button',{name:'核对方案并继续',exact:true}).count(),1);
    await review.click();
    const drawer=page.getByRole('dialog');await drawer.waitFor();
    assert.ok((await drawer.innerText()).includes(before.plans.at(-1).goal));
    assert.equal(await drawer.getByRole('button',{name:'确认方案并开始',exact:true}).isEnabled(),true);
  }
  if(before.execution?.actions?.can_accept) {
    assert.equal(await page.getByRole('button',{name:'确认结果',exact:true}).isEnabled(),true);
    await page.getByRole('button',{name:'查看改动与测试',exact:true}).click();
    const drawer=page.getByRole('dialog');
    await drawer.getByRole('heading',{name:'测试结果',exact:true}).waitFor();
    const v=before.execution.verification;
    assert.ok((await drawer.innerText()).includes(`${v.passed}/${v.total} 条验证命令通过`));
    assert.ok((await drawer.innerText()).includes('审查通过'));
    await drawer.getByRole('button',{name:'返回对话',exact:true}).click();
  }
  const publication=before.conversation.pending_action?.kind==='publication'
    ? before.events.find(e=>e.id===before.conversation.pending_action.source_id) : null;
  if(publication) {
    await page.getByRole('button',{name:'查看完整预览',exact:true}).last().click();
    const drawer=page.getByRole('dialog');await drawer.waitFor();
    assert.ok((await drawer.innerText()).includes(publication.payload.title));
    assert.ok((await drawer.innerText()).includes(publication.payload.body));
    const create=drawer.getByRole('button',{name:'创建草稿 PR',exact:true});
    if(Date.parse(publication.payload.expires_at)<=Date.now()) {
      assert.equal(await create.isDisabled(),true);
      assert.equal(await drawer.getByRole('button',{name:'沿用文案更新预览',exact:true}).isEnabled(),true);
    }else assert.equal(await create.isEnabled(),true);
    await drawer.getByRole('button',{name:'返回对话',exact:true}).click();
  }
  if(before.state==='merged') {
    assert.equal(before.conversation.user_state.label,'PR 已合并');
    assert.equal(before.conversation.pending_action,null);
    assert.equal(before.pr_lifecycle.snapshot.stage,'merged');
    assert.equal(before.pr_lifecycle.snapshot.pr.state,'closed');
    assert.equal(before.pr_lifecycle.snapshot.pr.merged,true);
    await page.locator('.conversation-agent-status').filter({hasText:'PR 已合并'}).waitFor();
    for(const name of ['查看审核预览','查看完整预览','确认结果','核对方案并继续'])
      assert.equal(await page.getByRole('button',{name,exact:true}).count(),0);
  }
  for(const width of [1440,390]) {
    await page.setViewportSize({width,height:900});
    if(before.execution?.actions?.can_accept || publication || before.state==='merged')
      await page.locator('.conversation-agent-messages').evaluate(el=>{el.scrollTop=el.scrollHeight;});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:`/tmp/contribos-question-deployed-${width}.png`});
  }
  const after=await (await page.request.get(url)).json();
  assert.equal(baseline(after),baseline(before));
  assert.deepEqual(errors,[]);assert.deepEqual(mutations,[]);
  console.log(JSON.stringify({status:'PASS',task,questions:source.payload.questions.length,
    awaiting_answers:awaiting,selections_retained:awaiting,resource_preview:!!before.native_resources?.preview?.current,
    plan_continuation:!!before.native_resources?.ready && !!before.execution?.actions?.can_execute,
    acceptance_ready:!!before.execution?.actions?.can_accept,
    publication_preview_checked:!!publication,
    merged_status_checked:before.state==='merged',
    history_unchanged:true,mutations:0,viewports:[1440,390]}));
} finally {await browser.close();}
