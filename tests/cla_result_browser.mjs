// Offline fixture verifies the real queued-job/timeline polling contract.
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page=await browser.newPage();const errors=[],mutations=[];
page.on('pageerror',e=>errors.push(e.message));
try {
  await page.goto('http://127.0.0.1:8765');
  await page.locator('#sidebar-tasks a').first().click();
  await page.locator('.conversation-plan').waitFor();
  const task=(await page.evaluate(()=>location.hash)).split('/').at(-1);
  const url=`http://127.0.0.1:8765/api/v1/tasks/${task}/workbench`;
  const source=await (await page.request.get(url)).json();
  let jobs=[],events=[],count=0;
  const cla={preview:{record_hash:'a'.repeat(64),publisher_account_hash:'b'.repeat(64),login:'contributor',
    pr_url:'https://github.com/fixture/repo/pull/7',target:{head_sha:'c'.repeat(40)},agreement_hash:'d'.repeat(64),
    agreement_url:`https://github.com/fixture/repo/blob/${'c'.repeat(40)}/CLA.md`,
    agreement_text:'Long legal agreement\n'.repeat(400),comment:'I have read the CLA Document and I hereby sign the CLA',
    consumed:true,status:'failed',expires_at:'2099-01-01T00:00:00Z'},
    confirmed:{},submitted:{comment_url:'https://github.com/fixture/repo/pull/7#issuecomment-19'},
    status:{status:'failed',observed_at:'2026-10-12T06:00:00Z'},unresolved:false};
  const view=()=>({...source,state:'draft_pr',planner_available:false,publisher_mode:'gh',
    busy:jobs.some(j=>['queued','running'].includes(j.state)),jobs,cla,
    events:[...source.events,{id:'published',sequence:100,kind:'publication_completed',payload:{html_url:cla.preview.pr_url}},...events],
    execution:{...source.execution,actions:{},publisher_account_hash:'b'.repeat(64),event_cursor:100+events.length,
      revision:JSON.stringify({jobs,events})}});
  await page.route(url,route=>route.fulfill({json:view()}));
  await page.route(url+'/timeline?*',route=>route.fulfill({json:{events,cursor:view().execution.event_cursor,execution:view().execution}}));
  await page.route(url+'/cla/check',route=>{
    mutations.push({url:route.request().url(),body:route.request().postDataJSON()});
    jobs.push({id:'check-'+(++count),kind:'cla_check',state:'queued'});
    return route.fulfill({status:202,json:{id:jobs.at(-1).id,state:'queued'}});
  });
  const complete=status=>{
    const job=jobs.at(-1);job.state='succeeded';
    cla.status={status,observed_at:`2026-10-12T06:${String(count).padStart(2,'0')}:00Z`,
      check_url:'https://github.com/fixture/repo/actions/runs/42/job/12'};
    events.push({id:'observed-'+count,sequence:100+events.length+1,kind:'cla_status_observed',job_id:job.id,payload:{...cla.status}});
  };
  await page.reload();
  const section=page.getByRole('region',{name:'CLA 协议签署',exact:true});
  await section.waitFor();
  assert.equal(await section.locator('details').getAttribute('open'),null);
  const check=page.getByRole('button',{name:'核对 CLA 结果',exact:true});
  await check.click();
  const loading=page.getByRole('button',{name:'正在核对 CLA 结果…',exact:true});
  await loading.waitFor();assert.equal(await loading.isDisabled(),true);
  assert.equal(await page.getByText('Publisher · CLA 核对结果',{exact:true}).count(),0);
  jobs.at(-1).state='running';await page.waitForTimeout(1200);complete('passed');
  const receipts=page.locator('.ai-message').filter({has:page.getByText('Publisher · CLA 核对结果',{exact:true})});
  await receipts.getByText('CLA 已通过。',{exact:true}).waitFor();
  assert.equal(await receipts.locator('time').getAttribute('datetime'),'2026-10-12T06:01:00Z');
  assert.equal(await receipts.getByRole('link',{name:'查看 GitHub CLA 检查',exact:true}).getAttribute('href'),cla.status.check_url);
  await page.reload();await receipts.getByText('CLA 已通过。',{exact:true}).waitFor();
  assert.equal(await receipts.count(),1); // Durable after reopening.
  for (const status of ['pending','not_run','unavailable','failed']) {
    await check.click();await loading.waitFor();complete(status);
    await page.locator(`[data-event-id="observed-${count}"]`).waitFor();
  }
  await check.click();await loading.waitFor();
  jobs.at(-1).state='failed';jobs.at(-1).error_message='GitHub 暂时不可用，请重试核对。';
  await section.getByText(jobs.at(-1).error_message,{exact:true}).waitFor();
  assert.equal(await check.isEnabled(),true);
  await check.click();await loading.waitFor();complete('passed');
  await page.locator(`[data-event-id="observed-${count}"]`).getByText('CLA 已通过。',{exact:true}).waitFor();
  for(const width of [1440,390]) {
    await page.setViewportSize({width,height:1000});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  }
  assert.ok(mutations.every(m=>m.url.endsWith('/cla/check') && JSON.stringify(m.body)==='{}'));
  assert.deepEqual(errors,[]);
  console.log('PASS: queued/running check, actual timeline revision polling, durable conversation receipts/time/source, all status states, failure/retry, long agreement collapsed, desktop/mobile; no signature writes');
} finally {await browser.close();}
