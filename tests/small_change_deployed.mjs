// Read-only inspection of real locally deployed recommendations.
import {createRequire} from "node:module";
import assert from "node:assert/strict";
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page = await browser.newPage({viewport:{width:1440,height:1000}});
const errors=[], mutations=[];
page.on("pageerror",e=>errors.push(e.message));
page.on("request",r=>{if(!["GET","HEAD","OPTIONS"].includes(r.method()))mutations.push(r.url());});
try {
  await page.goto("http://127.0.0.1:8000/#/discover");
  const all = await (await page.request.get("http://127.0.0.1:8000/api/v1/recommendations")).json();
  const toggle=page.locator("#small-change-toggle");
  await toggle.waitFor();
  await page.waitForFunction(()=>document.querySelector("#small-change-toggle").textContent.includes("("));
  const received=page.waitForResponse(r=>r.url().includes("/api/v1/recommendations?") && r.url().includes("small_change_only=true"));
  await toggle.click();
  const response=await received;
  assert.equal(response.status(),200);
  const filtered=await response.json();
  await page.waitForFunction(()=>document.querySelector("#small-change-toggle").getAttribute("aria-pressed")==="true");
  assert.equal(filtered.small_change_only,true);
  assert.equal(filtered.small_change_total,all.small_change_total);
  assert.equal(filtered.total,filtered.small_change_total);
  assert.ok(filtered.items.every(x=>x.change_scope.classification==="likely_small"));
  assert.equal(await page.locator("#opportunity-list .opportunity-card").count(),filtered.items.length);
  if(!filtered.items.length) await page.getByText("当前没有符合条件的小改动机会",{exact:true}).waitFor();
  assert.equal(await page.locator("#small-change-hint").isVisible(),true);
  // Returning to discovery must retain the filter and its latest matching count.
  await page.locator('[data-view-link="shortlist"]').click();
  await page.locator('[data-view-link="discover"]').click();
  assert.equal(await toggle.getAttribute("aria-pressed"),"true");
  assert.ok((await toggle.textContent()).includes(String(filtered.small_change_total)));
  assert.equal(await page.locator("#opportunity-list .opportunity-card").count(),filtered.items.length);
  if (!(await page.locator("#ai-screen-button").textContent()).includes("未配置")) {
    assert.ok((await page.locator("#ai-screen-status").textContent()).includes(filtered.pending_analysis_total === 0
      ? "当前范围没有待分析候选" : `当前范围有 ${filtered.pending_analysis_total} 个候选尚未分析`));
  }
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:1000});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:`/tmp/contribos-small-change-deployed-${width}.png`});
  }
  assert.deepEqual(errors,[]); assert.deepEqual(mutations,[]);
  console.log(JSON.stringify({status:"PASS",candidate_total:all.total,small_change_total:filtered.total,displayed:filtered.items.length,mutations:0,browser_errors:0}));
}finally{await browser.close();}
