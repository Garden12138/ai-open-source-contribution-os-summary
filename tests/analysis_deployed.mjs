// Read-only acceptance of a real persisted analysis; never triggers model calls.
import {createRequire} from "node:module";
import assert from "node:assert/strict";
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || "playwright");
const id = Number(process.env.ANALYSIS_OPPORTUNITY_ID || 329);
const browser = await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page = await browser.newPage({viewport:{width:1440,height:1000}});
const errors=[],mutations=[];
page.on("pageerror",e=>errors.push(e.message));
page.on("request",r=>{if(!["GET","HEAD","OPTIONS"].includes(r.method()))mutations.push(r.url());});
try {
  const base="http://127.0.0.1:8000";
  const history=await (await page.request.get(`${base}/api/v1/opportunities/${id}/analyses`)).json();
  assert.ok(history.versions.length>0,"A real report must exist before read-only UI acceptance");
  const version=history.versions[0];
  await page.goto(`${base}/#/discover`);
  await page.waitForFunction(()=>document.querySelector("#small-change-toggle").textContent.includes("("));
  await page.evaluate(id=>{location.hash=`#/opportunities/${id}`;},id);
  const root=page.locator("#opportunity-detail-content");
  await root.locator(".analysis-markdown-document").waitFor();
  await root.getByRole("button",{name:/制定贡献方案|打开贡献方案|制定方案/}).first().waitFor();
  const markdown=await page.request.get(`${base}/api/v1/opportunities/${id}/analyses/${version.id}/document`);
  assert.equal(markdown.status(),200);
  assert.ok((await markdown.text()).includes("验收"));
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:1000});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:`/tmp/contribos-analysis-${id}-${width}.png`});
  }
  assert.deepEqual(errors,[]);assert.deepEqual(mutations,[]);
  console.log(JSON.stringify({status:"PASS",opportunity_id:id,analysis_version_id:version.id,mutations:0,browser_errors:0}));
}finally{await browser.close();}
