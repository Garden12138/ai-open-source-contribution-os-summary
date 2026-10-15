// Disposable fixture only: fake GitHub probe, real browser encryption and APIs.
import {createRequire} from "node:module";
import assert from "node:assert/strict";
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page = await browser.newPage({viewport:{width:1440,height:1000}});
const errors = [], bodies = [];
const token = "ghp_BrowserPublisherCanary1234567890123456";
page.on("pageerror",e=>errors.push(e.message));
page.on("request",r=>{if(r.method()==="POST")bodies.push({url:r.url(),body:r.postData() || ""});});
try {
  // The first mutation must wait for CSRF even when the dashboard metadata
  // request has not yet completed.
  await page.route("**/api/v1/meta", async route => {
    const response = await route.fetch();
    await new Promise(resolve=>setTimeout(resolve,400));
    await route.fulfill({response});
  });
  await page.goto("http://127.0.0.1:8765/#/settings");
  const section = page.locator("#github-publisher-settings");
  await section.getByText("尚未连接 GitHub 发布账号。",{exact:true}).waitFor();
  await page.getByLabel("GitHub 发布 Token").fill(token);
  await section.getByRole("button",{name:"连接 GitHub 账号",exact:true}).click();
  await section.getByText("已连接 @browser-contributor · 可准备 Draft PR 发布预览。",{exact:true}).waitFor();
  assert.equal(await page.getByLabel("GitHub 发布 Token").inputValue(),"");
  const saved = bodies.find(r=>r.url.endsWith("/publisher-settings/credentials"));
  assert.ok(saved);
  const envelope = JSON.parse(saved.body).envelope;
  assert.ok(envelope.ciphertext && envelope.wrapped_key);
  assert.equal(bodies.some(r=>r.body.includes(token)),false);
  assert.equal((await page.content()).includes(token),false);
  const before = await (await page.request.get("http://127.0.0.1:8765/api/v1/publisher-settings")).json();
  await section.getByRole("button",{name:"重新检测账号",exact:true}).click();
  await page.waitForFunction(async old=>{
    const next=await (await fetch("/api/v1/publisher-settings")).json();
    return next.connected && next.account_hash!==old;
  },before.account_hash);
  await section.getByText("已连接 @browser-contributor · 可准备 Draft PR 发布预览。",{exact:true}).waitFor();
  const mutationsBeforeDisconnect = bodies.length;
  await section.getByRole("button",{name:"断开连接",exact:true}).click();
  assert.equal(bodies.length,mutationsBeforeDisconnect);
  await page.getByRole("group",{name:"断开 GitHub 发布连接确认"}).getByRole("button",{name:"确认断开",exact:true}).click();
  await section.getByText("尚未连接 GitHub 发布账号。",{exact:true}).waitFor();
  assert.equal((await (await page.request.get("http://127.0.0.1:8765/api/v1/publisher-settings")).json()).connected,false);
  assert.equal(bodies.some(r=>/publication|execute/.test(r.url)),false);
  for (const width of [1440,390]) {
    await page.setViewportSize({width,height:1000});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:`/tmp/contribos-publisher-settings-${width}.png`});
  }
  await page.route("**/api/v1/publisher-settings", route=>route.fulfill({json:{available:false,state:"unavailable",connected:false}}));
  await page.reload();
  await section.getByRole("button",{name:"重新检查服务",exact:true}).waitFor();
  assert.equal(await page.getByRole("heading",{name:"模型设置",exact:true}).isVisible(),true);
  assert.deepEqual(errors,[]);
  console.log("PASS: settings connection, browser sealing, persistent fake read-only probe, retest, explicit disconnect, service unavailable, desktop/mobile, no publication");
} catch(error) {
  console.error(await page.locator("#github-publisher-settings .settings-status").textContent());
  throw error;
} finally {await browser.close();}
