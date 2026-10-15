// Disposable fixture only; every mutation below is intercepted.
import {createRequire} from "node:module";
import assert from "node:assert/strict";
import {executionDetail} from "./execution_changes_fixtures.mjs";
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page = await browser.newPage();
const errors = [], mutations = [];
page.on("pageerror", e => errors.push(e.message));
try {
  const now = Date.parse("2026-10-12T00:00:00Z");
  await page.clock.install({time:now});
  await page.goto("http://127.0.0.1:8765");
  await page.locator("#sidebar-tasks a").first().click();
  await page.locator(".conversation-plan").waitFor();
  const taskId = (await page.evaluate(() => location.hash)).split("/").at(-1);
  const url = `http://127.0.0.1:8765/api/v1/tasks/${taskId}/workbench`;
  const source = await (await page.request.get(url)).json();
  const detail = executionDetail();
  let prepared = {id:"preview-1", kind:"publication_prepared", record_hash:"1".repeat(64), payload:{
    repository:"fixture/repo", fork:"fixture-contributor/repo", branch:"contribos/issue-1-fixture",
    publisher_account_hash:"a".repeat(64), title:"Verified change", body:"Verified body",
    nonce:"b".repeat(64), expires_at:new Date(now+8000).toISOString(),
    upstream_check:{status:"unchanged_workspace"},
  }};
  let revision = "preview", jobs = [];
  const view = () => ({...source, state:"ready", busy:false, publisher_mode:"gh", jobs,
    events:[...source.events, prepared], execution:{...source.execution, revision,
      publisher_account_hash:"a".repeat(64), latest_attempt_id:detail.id,
      acceptance:{available:true, binding_hash:"c".repeat(64), accepted:{record_hash:"d".repeat(64)},
        binding:{verify_result_hash:"e".repeat(64), review_hash:"f".repeat(64)}},
      verification:{status:"passed",passed:5,total:5},
      attempts:[{id:detail.id,number:1,review:"pass",verification:{status:"passed",passed:5,total:5}}]}});
  await page.route(url, route => route.fulfill({json:view()}));
  await page.route("**/api/v1/executions/*", route => route.fulfill({json:detail}));
  await page.route(url+"/publication", route => {
    const body = route.request().postDataJSON(); mutations.push({kind:"prepare", body});
    prepared = {...prepared, id:"preview-2", record_hash:"2".repeat(64),
      payload:{...prepared.payload, ...body, nonce:"c".repeat(64), expires_at:"2099-01-01T00:00:00Z"}};
    revision = "renewed";
    return route.fulfill({status:202,json:{id:"prepare-job",state:"queued"}});
  });
  await page.route(url+"/publication/confirm", route => {
    mutations.push({kind:"confirm",body:route.request().postDataJSON()});
    return route.fulfill({status:202,json:{id:"confirmed-fixture",state:"queued"}});
  });
  await page.route(url+"/messages", route => {
    mutations.push({kind:"refresh",body:route.request().postDataJSON()});
    return route.fulfill({status:202,json:{id:"refresh-fixture",state:"queued"}});
  });
  await page.reload();
  const publish = page.getByRole("button",{name:"确认 Fork、Push 并创建 Draft PR",exact:true});
  const consent = page.getByLabel("我已验收代码和测试，确认发布上述精确内容。",{exact:true});
  await publish.waitFor();
  await consent.check(); assert.equal(await publish.isEnabled(),true);
  await page.getByLabel("PR 标题",{exact:true}).fill("Edited title");
  await page.getByLabel("PR 正文",{exact:true}).fill("Edited body");
  assert.equal(await publish.isDisabled(),true);
  await page.clock.runFor(9000);
  await page.locator(".publication-preview-status").getByText("发布预览已过期",{exact:false}).waitFor();
  assert.equal(await publish.isDisabled(),true); assert.equal(await consent.isDisabled(),true);
  assert.equal(await page.getByLabel("PR 标题",{exact:true}).inputValue(),"Edited title");
  assert.equal(await page.getByLabel("PR 正文",{exact:true}).inputValue(),"Edited body");
  assert.deepEqual(mutations,[]);
  for (const width of [1440,390]) {
    await page.setViewportSize({width,height:1000});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.getByRole("button",{name:"重新准备发布预览",exact:true}).scrollIntoViewIfNeeded();
    await page.screenshot({path:`/tmp/contribos-publication-expired-${width}.png`});
  }
  await page.getByRole("button",{name:"重新准备发布预览",exact:true}).click();
  await page.getByText("预览有效至",{exact:false}).waitFor();
  assert.deepEqual(mutations[0],{kind:"prepare",body:{title:"Edited title",body:"Edited body"}});
  assert.equal(await consent.isChecked(),false); assert.equal(await consent.isEnabled(),true);
  assert.equal(await publish.isDisabled(),true);
  await consent.check(); await publish.click();
  assert.deepEqual(mutations.at(-1),{kind:"confirm",body:{intent_hash:"2".repeat(64),nonce:"c".repeat(64)}});
  jobs = [{id:"related-change",kind:"publication_prepare",state:"failed",error_message:"上游已修改已验证工作区或提交历史，请重新读取最新代码并确认方案，再在沙箱执行和验证。"}];
  revision = "related-change";
  await page.reload();
  await page.getByRole("button",{name:"🔄 立即重新读取最新代码并重规划",exact:true}).click();
  assert.equal(mutations.at(-1).kind,"refresh"); assert.equal(mutations.at(-1).body.refresh,true);
  assert.equal(mutations.filter(m=>m.kind==="confirm").length,1);
  assert.deepEqual(errors,[]);
  console.log("PASS: timer expiry disables consent, edited PR text preserved, explicit renewal/new nonce, fresh confirmation and upstream recovery, desktop/mobile");
} finally { await browser.close(); }
