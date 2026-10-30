// Read-only deployed UI checks. The added connection below is a browser fixture.
import {createRequire} from "node:module";
import assert from "node:assert/strict";
import {writeFileSync} from "node:fs";

const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || "playwright");
const origin=process.env.STUDIO_ORIGIN || "http://127.0.0.1:18090";
const browser=await chromium.launch({headless:true});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
const errors=[], mutations=[];
page.on("pageerror",e=>errors.push(e.message));
page.on("request",r=>{if(!["GET","HEAD","OPTIONS"].includes(r.method())) mutations.push(r.method());});
try {
  await page.goto(origin+"/#/settings");
  await page.getByRole("combobox",{name:"接口类型",exact:true}).selectOption("deepseek");
  const url=page.locator('input[name="base_url"]');
  assert.equal(await url.inputValue(),"https://api.deepseek.com");
  assert.equal(await url.evaluate(e=>e.readOnly),true);
  const secret=page.getByLabel("API Key",{exact:true});
  assert.equal(await secret.getAttribute("type"),"password");
  assert.equal(await secret.inputValue(),"");
  assert.equal(await secret.isEnabled(),true);
  assert.equal(await page.evaluate(()=>!!crypto.subtle),true);

  // No saved real DeepSeek credential is needed to check model-field behavior.
  await page.route("**/api/v1/model-settings",async route=>{
    const response=await route.fetch(), data=await response.json();
    data.connections.push({id:"browser-only-deepseek",scope:"connection:browser-only",payload:{name:"Browser-only DeepSeek",provider:"deepseek",base_url:"https://api.deepseek.com",credential_ref:null}});
    await route.fulfill({response,json:data});
  });
  await page.reload();
  await page.getByLabel("使用连接",{exact:true}).selectOption("browser-only-deepseek");
  assert.equal(await page.getByLabel("模型 ID",{exact:true}).inputValue(),"deepseek-v4-pro");
  const structured=page.getByLabel("结构化响应",{exact:true});
  assert.equal(await structured.inputValue(),"tools");
  assert.equal(await structured.isDisabled(),true);
  assert.equal(await page.getByLabel("Temperature（留空由服务商决定）",{exact:true}).isDisabled(),true);
  assert.equal(await page.getByLabel("推理强度（模型支持时）",{exact:true}).isEnabled(),true);
  for(const id of ["deepseek-v4-pro","deepseek-flash"]) assert.equal(await page.locator(`#studio-model-catalog option[value="${id}"]`).count(),1);
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  assert.deepEqual(errors,[]);
  assert.deepEqual(mutations,[]);
  await page.screenshot({path:"/tmp/upeve-deepseek-settings-mobile.png",fullPage:true});
  const report={classification:"deployed_UI_readiness_and_browser_component_fixture_not_native_Agent",origin,official_endpoint_locked:true,password_field_empty_enabled:true,webcrypto_available:true,model_field_fixture_only:true,model_ids:["deepseek-v4-pro","deepseek-flash"],thinking_capabilities_passed:true,mobile_overflow:false,browser_errors:errors,mutations:0,credentials_submitted:false,real_model_calls:0,native_acceptance:false};
  writeFileSync("/tmp/upeve-deepseek-settings-browser.json",JSON.stringify(report,null,2)+"\n");
  console.log(JSON.stringify(report));
} finally {await browser.close();}
