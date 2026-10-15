// Deployed read-only smoke: no credential entry, GitHub call or publication.
import {createRequire} from "node:module";
import assert from "node:assert/strict";
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page=await browser.newPage();
const errors=[],writes=[];
page.on("pageerror",e=>errors.push(e.message));
await page.route("**/api/**",route=>{
  if(!["GET","HEAD","OPTIONS"].includes(route.request().method())){writes.push(route.request().url());return route.abort();}
  return route.continue();
});
try {
  const taskId="82c0ec73-3be6-4499-b806-45cd31430722";
  await page.goto(`http://127.0.0.1:8000/#/tasks/${taskId}`);
  await page.getByRole("button",{name:"下载 PR 草稿",exact:true}).waitFor();
  await page.getByRole("link",{name:"前往设置 · 连接 GitHub 发布账号",exact:true}).click();
  const section=page.locator("#github-publisher-settings");
  await section.getByText("尚未连接 GitHub 发布账号。",{exact:true}).waitFor();
  const status=await (await page.request.get("http://127.0.0.1:8000/api/v1/publisher-settings")).json();
  assert.equal(status.available,true);assert.equal(status.connected,false);
  assert.equal(status.state,"disconnected");assert.equal(status.job_id,null);
  assert.equal(await page.getByLabel("GitHub 发布 Token").inputValue(),"");
  assert.equal(await section.getByRole("button",{name:"连接 GitHub 账号",exact:true}).isEnabled(),true);
  assert.equal(await page.getByRole("heading",{name:"模型设置",exact:true}).isVisible(),true);
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:1000});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:`/tmp/contribos-publisher-settings-live-${width}.png`});
  }
  assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);
  console.log("PASS: deployed Publisher available, disconnected without credentials, task-to-settings link, desktop/mobile, no writes or browser errors");
}finally{await browser.close();}
