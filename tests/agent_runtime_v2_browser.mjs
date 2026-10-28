// Browser contracts over the native UI. Intercepted data is not live acceptance.
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.CONTRIBOS_BROWSER_ORIGIN || 'http://127.0.0.1:18765';
const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page = await browser.newPage({viewport:{width:1440,height:1000}});
const errors=[]; page.on('pageerror',e=>errors.push(e.message));
try {
  await page.goto(origin); await page.locator('#sidebar-tasks a').first().click();
  await page.locator('.conversation-agent-message').first().waitFor();
  const task=(await page.evaluate(()=>location.hash)).split('/').at(-1);
  const url=`${origin}/api/v1/tasks/${task}/workbench`;
  const original=await (await page.request.get(url)).json();
  const artifact='a'.repeat(64); let reads=0;
  const runtime={version:'agent-runtime-v2',checkpoint:{state:'tools'},remaining_budget:{model_calls:7,reads:11,writes:3,checks:2},timeline:[
    {kind:'agent_v2_tool_started',created_at:'2026-10-28T00:00:00Z',payload:{name:'run_approved_checks'}},
    {kind:'agent_v2_tool_result',created_at:'2026-10-28T00:00:01Z',payload:{artifact_id:artifact}},
    {kind:'agent_v2_blocked',payload:{failure:{category:'environment',safe_reason_code:'verification_command_not_executed',next_allowed_action:'inspect_diagnostic'}}}
  ]};
  await page.route(url,route=>route.fulfill({json:{...original,execution:{...original.execution,agent_runtime_v2:runtime}}}));
  await page.route(url+'/runtime/artifacts/'+artifact+'*',route=>{
    reads++; const offset=new URL(route.request().url()).searchParams.get('offset');
    return route.fulfill({json:{text:offset?'真实失败的后续证据':'<script>window.toolInjected=true</script>真实测试失败',next_offset:offset?null:64,total_bytes:100}});
  });
  await page.reload();
  for(const viewport of [{width:1440,height:1000},{width:390,height:844}]) {
    await page.setViewportSize(viewport);
    await page.getByRole('button',{name:'工具与测试记录',exact:true}).click();
    await page.getByRole('list',{name:'工具时间线'}).waitFor();
    assert.match(await page.locator('dialog').innerText(),/run_approved_checks/);
    assert.match(await page.locator('dialog').innerText(),/剩余模型调用 7/);
    await page.getByRole('button',{name:'查看结果',exact:true}).click();
    await page.getByRole('button',{name:'继续读取证据',exact:true}).click();
    await page.getByText('真实失败的后续证据',{exact:false}).waitFor();
    assert.equal(await page.evaluate(()=>Boolean(window.toolInjected)),false);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.getByRole('button',{name:'返回对话',exact:true}).click();
  }
  assert.equal(reads,4); assert.deepEqual(errors,[]);
  console.log(JSON.stringify({classification:'browser_contract_mock_data',passed:2,failed:0,artifact_page_reads:reads}));
} finally {await browser.close();}
