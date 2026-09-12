// Run against the disposable, offline tests.studio_browser_server fixture.
import {createRequire} from "node:module";
import assert from "node:assert/strict";
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await chromium.launch({headless:true});
const page = await browser.newPage();
const errors = [];
page.on("pageerror", error => errors.push(error.message));
try {
  await page.goto("http://127.0.0.1:8765");
  await page.locator("#sidebar-tasks a").first().click();
  const taskId = (await page.evaluate(() => location.hash)).split("/").at(-1);
  await page.getByLabel("更多任务操作").click();
  await page.getByRole("button", {name:"归档任务", exact:true}).click();
  await page.waitForURL("**/#/contributions");
  await page.getByLabel("查看已归档任务").check();
  await page.locator("#task-history").getByRole("button", {name:"恢复任务"}).waitFor();
  assert.equal(await page.locator("#sidebar-tasks a").count(), 0);
  await page.reload();
  await page.getByLabel("查看已归档任务").check();
  await page.locator("#task-history").getByRole("button", {name:"恢复任务"}).click();
  await page.locator("#task-history").getByText("还没有归档任务。").waitFor();
  await page.locator("#sidebar-tasks a").first().waitFor();
  await page.setViewportSize({width:390, height:844});
  await page.getByLabel("查看已归档任务").uncheck();
  await page.locator("#task-history").getByRole("button", {name:"归档任务"}).click();
  await page.locator("#task-history").getByText("还没有贡献任务。").waitFor();
  await page.getByLabel("查看已归档任务").check();
  const remove = page.locator("#task-history").getByRole("button", {name:"删除任务"});
  page.once("dialog", dialog => dialog.dismiss());
  await remove.click();
  assert.equal(await remove.count(), 1);
  page.once("dialog", dialog => dialog.accept());
  await remove.click();
  await page.locator("#task-history").getByText("还没有归档任务。").waitFor();
  await page.reload();
  await page.getByLabel("查看已归档任务").check();
  await page.locator("#task-history").getByText("还没有归档任务。").waitFor();
  assert.equal((await page.request.get(`http://127.0.0.1:8765/api/v1/tasks/${taskId}`)).status(), 404);
  assert.equal(await page.locator("#sidebar-tasks a").count(), 0);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  assert.deepEqual(errors, []);
  console.log("Task archive, reload, restore, mobile archive, delete cancellation, delete and persistence passed.");
} finally {
  await browser.close();
}
