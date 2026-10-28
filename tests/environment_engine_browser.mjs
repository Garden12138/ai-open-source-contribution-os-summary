// Controlled API projection in the actual native UI; never a native delivery.
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.CONTRIBOS_BROWSER_ORIGIN || 'http://127.0.0.1:18767';
const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page = await browser.newPage();
const errors=[]; page.on('pageerror', e => errors.push(e.message));
try {
  await page.goto(origin);
  await page.locator('#sidebar-tasks a').first().click();
  await page.locator('.conversation-agent-message').first().waitFor();
  const task = (await page.evaluate(() => location.hash)).split('/').at(-1);
  const endpoint = `${origin}/api/v1/tasks/${task}/workbench`;
  const original = await (await page.request.get(endpoint)).json();
  let blocked = false;
  await page.route(endpoint, route => route.fulfill({json:{...original, execution:{...original.execution,
    environment_engine:{version:'environment-engine-view-v1',contract_hash:'a'.repeat(64),evidence_refs:{},executables:[],
      stages:{discovery:'recorded',resolution:blocked?'unsupported':'static_admitted',preparation:'unverified',entrypoints:'unverified',baseline:'unverified',coding:'not_started',review:'unverified'},
      gaps:blocked?[{reason_code:'unsupported_dynamic_installer',paths:['tools/check.js']}]:[]}}}}));
  for (const viewport of [{width:1440,height:1000},{width:390,height:844}]) {
    await page.setViewportSize(viewport);
    blocked=false; await page.reload();
    await page.getByText('验证环境进度', {exact:false}).first().waitFor();
    let text=await page.locator('#conversation-messages').count() ? await page.locator('#conversation-messages').innerText() : await page.locator('main').innerText();
    assert.match(text,/依赖图：静态通过/);
    assert.match(text,/修改前测试：尚未验证/);
    assert.match(text,/编码：尚未开始/);
    assert.match(text,/真实工具和修改前测试尚未验证/);
    blocked=true; await page.reload();
    await page.getByText('unsupported_dynamic_installer', {exact:false}).first().waitFor();
    assert.match(await page.locator('main').innerText(),/tools\/check\.js/);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
  }
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({classification:'browser_component_intercepted_api',viewports:2,states:2,passed:true,native:false}));
} finally {await browser.close();}
