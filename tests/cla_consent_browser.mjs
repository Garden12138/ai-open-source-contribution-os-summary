// Offline disposable fixture. All mutations are intercepted; no legal signature is posted.
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page=await browser.newPage();
const errors=[], writes=[];
page.on('pageerror',e=>errors.push(e.message));
try {
  await page.goto('http://127.0.0.1:8765');
  await page.locator('#sidebar-tasks a').first().click();
  await page.locator('.conversation-plan').waitFor();
  const task=(await page.evaluate(()=>location.hash)).split('/').at(-1);
  const url=`http://127.0.0.1:8765/api/v1/tasks/${task}/workbench`;
  const source=await (await page.request.get(url)).json();
  const agreement='Complete license agreement\n<script>window.claInjected = true</script>\nRead the terms before agreeing.';
  const preview={record_hash:'a'.repeat(64),nonce:'b'.repeat(64),agreement_hash:'c'.repeat(64),
    publisher_account_hash:'d'.repeat(64),login:'contributor',pr_url:'https://github.com/fixture/repo/pull/7',
    target:{head_sha:'e'.repeat(40)},agreement_url:`https://github.com/fixture/repo/blob/${'f'.repeat(40)}/CLA.md`,
    agreement_text:agreement,comment:'I have read the CLA Document and I hereby sign the CLA',
    acknowledgement:'我已阅读并同意上述 CLA 协议（SHA-256：'+ 'c'.repeat(64)+'），授权 GitHub 账号 contributor 向 https://github.com/fixture/repo/pull/7 提交所展示的完整签署评论。',
    status:'failed',expires_at:'2099-01-01T00:00:00Z',consumed:false,existing_comment:null};
  let cla={preview:null,submitted:null,status:null,unresolved:false}, account='d'.repeat(64), previews=0;
  const view=()=>({...source,state:'draft_pr',busy:false,planner_available:false,publisher_mode:'gh',jobs:[],
    events:[...source.events,{id:'published',sequence:100,kind:'publication_completed',payload:{html_url:preview.pr_url}}],
    execution:{...source.execution,publisher_account_hash:account,actions:{}},cla});
  await page.route(url,route=>route.fulfill({json:view()}));
  await page.route(url+'/timeline?*',route=>route.fulfill({json:{events:[],cursor:100,
    execution:{...view().execution,revision:'cla-'+JSON.stringify(cla)}}}));
  await page.route(url+'/cla/prepare',route=>{
    writes.push({kind:'prepare',body:route.request().postDataJSON()});
    preview.record_hash=(++previews).toString(16).repeat(64);
    cla={...cla,preview:{...preview}}; return route.fulfill({status:202,json:{id:'read',state:'queued'}});
  });
  await page.route(url+'/cla/confirm',route=>{
    writes.push({kind:'confirm',body:route.request().postDataJSON()});
    cla={...cla,preview:{...cla.preview,consumed:true},submitted:{comment_id:19,comment_url:preview.pr_url+'#issuecomment-19'},status:{status:'failed'}};
    return route.fulfill({status:202,json:{id:'sign',state:'queued'}});
  });
  await page.route(url+'/cla/check',route=>{
    writes.push({kind:'check',body:route.request().postDataJSON()});
    cla={...cla,status:{status:'passed'}}; return route.fulfill({status:202,json:{id:'check',state:'queued'}});
  });
  await page.reload();
  await page.getByRole('button',{name:'读取 CLA 协议',exact:true}).click();
  await page.getByText(agreement,{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>window.claInjected),undefined);
  const label='我已阅读并同意上述固定版本的 CLA 协议，并有权以所展示账号签署。';
  const consent=page.getByLabel(label,{exact:true});
  assert.equal(await consent.isChecked(),false);
  const textarea=page.getByLabel('发送给贡献助手的消息',{exact:true});
  await textarea.fill('确认');
  assert.equal(writes.filter(w=>w.kind==='confirm').length,0);
  await page.getByRole('button',{name:'在对话中确认签署',exact:true}).click();
  await page.getByText('请先阅读并勾选同意当前 CLA 协议。',{exact:true}).waitFor();
  await consent.check();
  await page.getByRole('button',{name:'在对话中确认签署',exact:true}).click();
  assert.equal(await textarea.inputValue(),preview.acknowledgement);
  const submit=page.getByRole('button',{name:'确认并提交 CLA 签署',exact:true});
  assert.equal(await submit.isEnabled(),true); // No model provider required.
  await consent.uncheck(); assert.equal(await submit.isDisabled(),true);
  await consent.check();
  // Expiry is enforced on timer without requiring a page reload.
  cla.preview.expires_at=new Date(Date.now()+500).toISOString();
  await page.waitForTimeout(6500);
  assert.equal(await submit.isDisabled(),true);
  assert.equal(writes.filter(w=>w.kind==='confirm').length,0);
  await page.getByRole('button',{name:'重新读取 CLA 协议',exact:true}).click();
  assert.equal(await consent.isChecked(),false);
  // New mount and account change never retain legal consent.
  account='9'.repeat(64); await page.reload();
  assert.equal(await page.getByRole('button',{name:'在对话中确认签署',exact:true}).count(),0);
  account='d'.repeat(64); await page.reload();
  await consent.check(); await page.getByRole('button',{name:'在对话中确认签署',exact:true}).click();
  for (const width of [1440,390]) {
    await page.setViewportSize({width,height:1000});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  }
  await submit.click();
  await page.getByText('签署评论已提交；是否通过以 GitHub CLA 检查为准。',{exact:true}).waitFor();
  const sent=writes.filter(w=>w.kind==='confirm');
  assert.equal(sent.length,1);
  assert.deepEqual(sent[0].body,{preview_hash:preview.record_hash,nonce:preview.nonce,
    agreement_hash:preview.agreement_hash,acknowledgement:preview.acknowledgement,read_and_agree:true});
  await page.getByRole('button',{name:'核对 CLA 结果',exact:true}).click();
  await page.getByText('已发布提交的 GitHub CLA 检查已通过。',{exact:true}).waitFor();
  assert.equal(writes.filter(w=>w.kind==='confirm').length,1);
  assert.deepEqual(errors,[]);
  console.log('PASS: full safe agreement, unchecked separate consent, plain confirmation never signs, no model requirement, expiry/account reset, exact payload, submitted vs passed, desktop/mobile');
} catch (error) {
  console.error('Browser errors:', errors);
  console.error((await page.locator('.ai-workbench').innerText()).slice(-7000));
  throw error;
} finally {await browser.close();}
