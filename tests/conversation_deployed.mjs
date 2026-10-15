// Read-only smoke test of the deployed service; never starts a model or execution.
import {createRequire} from "node:module";
import assert from "node:assert/strict";
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page = await browser.newPage({viewport:{width:1440,height:1000}});
const errors=[], mutations=[];
page.on("pageerror",e=>errors.push(e.message));
page.on("request",r=>{if(!["GET","HEAD","OPTIONS"].includes(r.method()))mutations.push(r.url());});
try {
 await page.goto("http://127.0.0.1:8000");
 const existingTaskId = process.env.CONTRIBOS_TASK_ID || "9727c1c6-04d4-4422-a8b0-e47b61517816";
 await page.evaluate(id => {location.hash=`#/tasks/${id}`;}, existingTaskId);
 await page.locator(".conversation-plan").waitFor();
 await page.locator(".conversation-readiness").waitFor();
 assert.equal(await page.getByLabel("消息用途").inputValue(),"question");
 assert.equal(await page.getByRole("button",{name:"确认方案并执行",exact:true}).isDisabled(),true);
 const text=await page.locator(".conversation-environment").textContent();
 assert.ok(text.includes("专用环境适配"));
 const selector=page.getByLabel("查看执行轮次");
 const values=await selector.locator("option").evaluateAll(options=>options.map(o=>o.value).filter(Boolean));
 if(values.length){
  await selector.selectOption(values.at(-1));
  await page.locator(".conversation-outcome").getByText("验证环境未就绪",{exact:false}).waitFor();
  assert.ok((await page.locator(".conversation-outcome").textContent()).includes("filetype"));
 }
 for(const width of [1440,390]){
  await page.setViewportSize({width,height:1000});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.screenshot({path:`/tmp/contribos-conversation-deployed-${width}.png`});
 }
 assert.deepEqual(errors,[]);assert.deepEqual(mutations,[]);
 console.log("PASS: deployed read-only desktop/mobile, environment limitations, historical failed tests, no browser errors or mutations");
}finally{await browser.close();}
