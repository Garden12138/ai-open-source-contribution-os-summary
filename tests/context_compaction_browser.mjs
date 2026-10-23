// Disposable fixture only: no model calls, execution or external writes.
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page=await browser.newPage({viewport:{width:1440,height:960}}),errors=[],mutations=[];
page.on('pageerror',e=>errors.push(e.message));
page.on('request',r=>{if(!['GET','HEAD'].includes(r.method()))mutations.push(r.url());});
try {
  await page.goto('http://127.0.0.1:8765');
  await page.locator('#sidebar-tasks a').first().click();
  await page.locator('.conversation-agent-status').waitFor();
  const task=(await page.evaluate(()=>location.hash)).split('/').at(-1);
  const url=`http://127.0.0.1:8765/api/v1/tasks/${task}/workbench`;
  const original=await (await page.request.get(url)).json();
  await page.route(url,route=>route.fulfill({json:{...original,
    execution:{...original.execution,context_management:{status:'compacted',original_bytes:400000,
      prompt_bytes:100000,saved_bytes:300000,usage:{calls:23,input_tokens:2079795,output_tokens:52195,
      limits:{calls:32,input_tokens:2000000,output_tokens:100000}}}}}}));
  await page.reload();
  const status=page.locator('.conversation-agent-status');
  await status.getByText('文本体积减少 75%',{exact:false}).waitFor();
  await status.getByText('累计预算已用尽',{exact:false}).waitFor();
  assert.equal(await status.getAttribute('aria-live'),'polite');
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:960});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:`/tmp/contribos-context-compaction-${width}.png`});
  }
  assert.deepEqual(errors,[]);assert.deepEqual(mutations,[]);
  console.log(JSON.stringify({ok:true,compressionVisible:true,budgetDistinct:true,desktop:true,mobile:true,modelCalls:0,realWrites:0}));
}finally{await browser.close();}
