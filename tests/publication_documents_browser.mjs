// Disposable fixture; all publication mutations are intercepted.
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
import {executionDetail} from './execution_changes_fixtures.mjs';
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page = await browser.newPage();
const errors = [], mutations = [];
page.on('pageerror', error => errors.push(error.message));
try {
  await page.goto('http://127.0.0.1:8765');
  await page.locator('#sidebar-tasks a').first().click();
  await page.locator('.conversation-plan').waitFor();
  const taskId = (await page.evaluate(() => location.hash)).split('/').at(-1);
  const url = `http://127.0.0.1:8765/api/v1/tasks/${taskId}/workbench`;
  const source = await (await page.request.get(url)).json();
  const detail = executionDetail();
  let events = [], jobs = [], busy = false, revision = 'initial';
  const doc = {id:'document-1',sequence:300,kind:'publication_document_ready',record_hash:'1'.repeat(64),payload:{
    title:'fix(openai): accumulate streamed refusal and reasoning content',
    body:'## Summary\nAccumulate streamed content.\n\n## Testing\nSandbox tests passed.\n\n- [ ] Review contribution guidelines.',
    template_path:'.github/PULL_REQUEST_TEMPLATE.md', notes:['Remote CI has not run.'],
    requirements:{sources:[{repository:'fixture/repo',commit:'a'.repeat(40),path:'.github/PULL_REQUEST_TEMPLATE.md'}],
      title_rules:{conventional:true,types:['fix','docs'],manual:[]}},
  }};
  const view = () => ({...source,state:'ready',busy,publisher_mode:'gh',jobs,events:[...source.events,...events],
    execution:{...source.execution,revision,publisher_account_hash:'a'.repeat(64),latest_attempt_id:detail.id,
      acceptance:{available:true,binding_hash:'b'.repeat(64),accepted:{record_hash:'c'.repeat(64)},binding:{}},
      verification:{status:'passed',passed:5,total:5},
      attempts:[{id:detail.id,number:1,review:'pass',verification:{status:'passed',passed:5,total:5}}]}});
  await page.route(url, route=>route.fulfill({json:view()}));
  await page.route('**/api/v1/executions/*',route=>route.fulfill({json:detail}));
  await page.route(url+'/publication/document',route=>{
    mutations.push({kind:'document',body:route.request().postDataJSON()});
    busy=true; revision='reading'; jobs=[{id:'reading',kind:'publication_requirements',state:'running'}];
    return route.fulfill({status:202,json:{id:'reading',state:'queued'}});
  });
  await page.route(url+'/publication',route=>{
    const body=route.request().postDataJSON(); mutations.push({kind:'prepare',body});
    events=[doc,{id:'preview-1',sequence:301,kind:'publication_prepared',record_hash:'2'.repeat(64),
      payload:{...body,nonce:'d'.repeat(64),expires_at:'2099-01-01T00:00:00Z',publisher_account_hash:'a'.repeat(64),
        repository:'fixture/repo',fork:'contributor/repo',branch:'contribos/issue-1-fixture'}}];
    revision='prepared';
    return route.fulfill({status:202,json:{id:'preview-job',state:'queued'}});
  });
  await page.route(url+'/publication/confirm',route=>{
    mutations.push({kind:'confirm',body:route.request().postDataJSON()});
    return route.fulfill({status:202,json:{id:'write-job',state:'queued'}});
  });
  await page.reload();
  const title=page.getByLabel('PR 标题',{exact:true});
  const body=page.getByLabel('PR 正文',{exact:true});
  const prepare=page.getByRole('button',{name:'准备发布预览',exact:true});
  await prepare.waitFor();
  assert.equal(await title.inputValue(),''); assert.equal(await body.inputValue(),'');
  assert.equal(await prepare.isDisabled(),true);
  await page.getByRole('button',{name:'读取仓库规范并生成英文草稿',exact:true}).click();
  await page.getByRole('button',{name:'正在读取规范并生成英文草稿…',exact:true}).waitFor();
  assert.equal(await title.getAttribute('readonly'),'');
  assert.equal(await prepare.isDisabled(),true);
  assert.deepEqual(mutations,[{kind:'document',body:{}}]);
  jobs=[{id:'writing',kind:'publication_document',state:'running'}]; revision='writing';
  await page.reload();
  await page.getByRole('button',{name:'正在读取规范并生成英文草稿…',exact:true}).waitFor();
  busy=false; jobs=[]; events=[doc]; revision='written';
  await page.reload(); await title.waitFor();
  assert.equal(await title.inputValue(),doc.payload.title); assert.equal(await body.inputValue(),doc.payload.body);
  await page.getByText('仓库规范与发布前人工事项',{exact:true}).click();
  await page.getByText('CLA 必须由你阅读并亲自签署',{exact:false}).waitFor();
  assert.equal(await page.getByRole('link',{name:'fixture/repo/.github/PULL_REQUEST_TEMPLATE.md'}).getAttribute('href'),
    `https://github.com/fixture/repo/blob/${'a'.repeat(40)}/.github/PULL_REQUEST_TEMPLATE.md`);
  for (const width of [1440,390]) {
    await page.setViewportSize({width,height:1000});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  }
  await title.fill('fix(openai): retain streamed content');
  await prepare.click();
  const consent=page.getByLabel('我已验收代码和测试，确认发布上述精确内容。',{exact:true});
  await consent.waitFor(); assert.equal(await consent.isChecked(),false);
  assert.equal(mutations[1].body.document_hash,doc.record_hash);
  assert.equal(mutations[1].body.template_path,doc.payload.template_path);
  const publish=page.getByRole('button',{name:'确认 Fork、Push 并创建 Draft PR',exact:true});
  assert.equal(await publish.isDisabled(),true);
  await consent.check(); assert.equal(await publish.isEnabled(),true);
  await body.fill(doc.payload.body+'\nAdditional details.');
  assert.equal(await publish.isDisabled(),true); assert.equal(await consent.isChecked(),false);
  assert.equal(mutations.filter(x=>x.kind==='confirm').length,0);
  assert.deepEqual(errors,[]);
  console.log('PASS: blank initial public text, repository-guided English generation, busy states, editable document, pinned sources/manual CLA/CI notes, exact document/template binding and fresh consent, desktop/mobile');
} finally { await browser.close(); }
