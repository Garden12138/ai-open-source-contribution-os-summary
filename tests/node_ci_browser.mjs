// Read-only native UI contract check; intercepted data is not native execution.
import {createRequire} from 'node:module';
import {readFileSync, writeFileSync} from 'node:fs';
import assert from 'node:assert/strict';
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.CONTRIBOS_BROWSER_ORIGIN || 'http://127.0.0.1:18090';
const task = process.env.CONTRIBOS_BROWSER_TASK;
assert.ok(task, 'A dedicated isolated task is required');
const sample = JSON.parse(readFileSync(process.env.CI_TOOLCHAIN_EVIDENCE, 'utf8'));
const declarations = sample.new_environment.verification_contract.preparation.toolchain_declarations;
const endpoint = `${origin}/api/v1/tasks/${task}/workbench`;
const browser = await chromium.launch({headless:true});
const page = await browser.newPage();
const errors = [], mutations = [];
page.on('pageerror', error => errors.push(error.message));
try {
  const response = await page.request.get(endpoint);
  assert.equal(response.status(), 200);
  const original = await response.json();
  await page.route('**/*', route => {
    const request = route.request();
    if (!['GET','HEAD'].includes(request.method())) {
      mutations.push(request.method());
      return route.abort();
    }
    if (!request.url().startsWith(origin + '/')) return route.abort();
    if (request.url() === endpoint) return route.fulfill({json:{...original,
      execution:{...original.execution, environment_engine:{...original.execution.environment_engine,
        toolchain_declarations:declarations}}}});
    return route.continue();
  });
  for (const viewport of [{width:1440,height:1000},{width:390,height:844}]) {
    await page.setViewportSize(viewport);
    await page.goto(`${origin}/#/tasks/${task}`);
    await page.getByText('CI 工具约束', {exact:false}).first().waitFor();
    const text = await page.locator('main').innerText();
    assert.match(text, /pnpm：10/);
    assert.match(text, /node：22/);
    assert.match(text, /\.github\/workflows\/ci\.yml:/);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  }
  assert.deepEqual(errors, []);
  assert.deepEqual(mutations, []);
  const result = {classification:'read_only_browser_component_API_intercept', native:false,
    viewports:2, CI_constraints_visible:true, source_path_and_line_visible:true,
    page_errors:errors, mutation_requests:mutations, passed:true};
  if (process.env.CI_TOOLCHAIN_BROWSER_RESULT) writeFileSync(process.env.CI_TOOLCHAIN_BROWSER_RESULT, JSON.stringify(result,null,2)+'\n');
  console.log(JSON.stringify(result));
} finally {
  await browser.close();
}
