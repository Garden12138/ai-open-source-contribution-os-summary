// Read-only UI contract; recommendation responses are controlled fixture data.
import {createRequire} from "node:module";
import assert from "node:assert/strict";
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page = await browser.newPage({viewport:{width:1440,height:1000}});
const errors = [], mutations = [], queries = [];
page.on("pageerror", e => errors.push(e.message));
page.on("request", r => {if (!["GET","HEAD","OPTIONS"].includes(r.method())) mutations.push(r.url());});
try {
  await page.goto("http://127.0.0.1:8765/#/discover");
  const source = await (await page.request.get("http://127.0.0.1:8765/api/v1/recommendations")).json();
  const small = {...source.items[0], change_scope:{classification:"likely_small",reason_codes:["text_or_link_fix"],exclusion_codes:[],rule_version:"change-scope-v1"},
    opportunity:{...source.items[0].opportunity,title:"Fix typo in README"}};
  const second = source.items[1] || source.items[0];
  const large = {...second, analysis_status:"analyzed",analysis_recommendation:"pursue",
    change_scope:{classification:"excluded",reason_codes:[],exclusion_codes:["large_scope"],rule_version:"change-scope-v1"},
    opportunity:{...second.opportunity,id:small.opportunity.id+100,title:"Migrate all acceptance tests to Playwright"}};
  let failNext = false, empty = false;
  await page.route(/\/api\/v1\/recommendations(?:\?.*)?$/, route => {
    const query = new URL(route.request().url()).searchParams;
    queries.push(query.toString());
    if (failNext) {failNext=false; return route.fulfill({status:503,json:{detail:"暂时无法加载筛选结果"}});}
    const onlySmall = query.get("small_change_only") === "true";
    const recommended = query.get("analysis_filter") === "recommended";
    const items = empty ? [] : onlySmall ? (recommended ? [] : [small]) : recommended ? [large] : [small,large];
    return route.fulfill({json:{...source,items,total:items.length,small_change_only:onlySmall,
      small_change_total:empty ? 0 : 1,analyzed_total:onlySmall ? 0 : 1,recommended_total:onlySmall ? 0 : 1,pending_analysis_total:1}});
  });
  await page.reload();
  const toggle = page.locator("#small-change-toggle");
  const cards = page.locator("#opportunity-list .opportunity-card");
  await cards.first().waitFor();
  assert.equal(await cards.count(),2);
  await toggle.click();
  await page.waitForFunction(() => document.querySelector("#small-change-toggle").getAttribute("aria-pressed") === "true");
  assert.equal(await cards.count(),1);
  assert.ok(queries.at(-1).includes("small_change_only=true"));
  assert.ok((await cards.textContent()).includes("Fix typo"));
  assert.ok((await page.locator(".small-change-evidence").textContent()).includes("文案、拼写或链接修复"));
  assert.equal(await page.locator("#small-change-hint").isVisible(),true);
  failNext = true;
  await toggle.click();
  await page.getByText("暂时无法加载筛选结果",{exact:false}).waitFor();
  assert.equal(await toggle.getAttribute("aria-pressed"),"true");
  assert.equal(await toggle.isDisabled(),false);
  assert.equal(await cards.count(),1);
  await toggle.click();
  await page.waitForFunction(() => document.querySelectorAll("#opportunity-list .opportunity-card").length === 2);
  // The AI recommendation condition must compose with the small-change filter.
  await page.locator("#ai-filter-button").click();
  await page.waitForFunction(() => document.querySelectorAll("#opportunity-list .opportunity-card").length === 1);
  await toggle.click();
  await page.getByText("当前没有符合条件的小改动机会",{exact:true}).waitFor();
  assert.ok(queries.at(-1).includes("analysis_filter=recommended"));
  assert.ok(queries.at(-1).includes("small_change_only=true"));
  assert.equal(await cards.count(),0);
  await page.locator("#ai-filter-button").click();
  await cards.first().waitFor();
  for (const width of [1440,390]) {
    await page.setViewportSize({width,height:1000});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true);
    await page.screenshot({path:`/tmp/contribos-small-change-${width}.png`});
  }
  empty = true;
  await toggle.click();
  await toggle.click();
  await page.getByText("当前没有符合条件的小改动机会",{exact:true}).waitFor();
  assert.deepEqual(errors,[]);
  assert.deepEqual(mutations,[]);
  console.log("PASS: server filter query, matching reasons, combined filters, empty/error recovery, desktop/mobile; no mutations");
} finally {await browser.close();}
