// Deterministic UI contract: no real resource download or implementation starts.
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
const page=await browser.newPage({viewport:{width:1440,height:900}});
const errors=[], mutations=[];
page.on('pageerror',e=>errors.push(e.message));
try {
  await page.goto('http://127.0.0.1:8765');
  await page.locator('#sidebar-tasks a').first().click();
  await page.locator('.conversation-agent-message').first().waitFor();
  const task=(await page.evaluate(()=>location.hash)).split('/').at(-1);
  const url=`http://127.0.0.1:8765/api/v1/tasks/${task}/workbench`;
  const source=await (await page.request.get(url)).json();
  let resources={available:true,ready:false,status:'awaiting_consent',preview:null}, busy=false, reject=true;
  const snapshot=()=>({...source,state:'planning',busy,planner_available:true,native_resources:resources,
    execution:{...source.execution,actions:{},readiness:{blockers:[]}},
    conversation:{...source.conversation,pending_count:0,paused:false,pending_action:{kind:'native_resource',requires_confirmation:true},
      user_state:{label:'等待资源授权',busy,can_stop:busy}}});
  await page.route(url,route=>route.fulfill({json:snapshot()}));
  await page.route(url+'/native-resources/prepare',route=>{
    mutations.push({kind:'prepare',body:route.request().postDataJSON()});
    resources.preview={record_hash:'a'.repeat(64),nonce:'b'.repeat(64),current:true,consumed:false,
      expires_at:new Date(Date.now()+1800000).toISOString(),resource:{name:'FFmpeg / ffprobe',version:'fixture-fixed-version',platform:'linux/arm64',
        purpose:'供项目测试在断网环境使用固定媒体工具',url:'https://github.com/BtbN/FFmpeg-Builds/releases/download/fixed/ffmpeg.tar.xz',
        sha256:'c'.repeat(64),download_bytes:256*1024**2,expanded_bytes:512*1024**2,timeout_seconds:600}};
    return route.fulfill({json:resources.preview});
  });
  await page.route(url+'/native-resources/confirm',route=>{
    mutations.push({kind:'confirm',body:route.request().postDataJSON()});
    if(reject){reject=false;return route.fulfill({status:503,json:{detail:'下载请求暂未保存，请重新提交。'}});}
    resources={...resources,status:'queued',preview:{...resources.preview,consumed:true}};busy=true;
    return route.fulfill({status:202,json:{id:'native-job',state:'queued'}});
  });
  await page.route(url+'/execute',()=>{throw new Error('Resource consent must never start implementation');});
  await page.reload();
  await page.getByRole('button',{name:'准备资源下载授权',exact:true}).waitFor();
  assert.equal(mutations.length,0);
  assert.equal(await page.getByRole('button',{name:'授权下载这些资源',exact:true}).count(),0);
  await page.getByRole('button',{name:'准备资源下载授权',exact:true}).click();
  const dialog=page.getByRole('dialog');await dialog.waitFor();
  assert.deepEqual(mutations.map(x=>x.kind),['prepare']);
  assert.ok((await dialog.innerText()).includes('256 MiB'));
  assert.ok((await dialog.innerText()).includes('c'.repeat(64)));
  assert.ok((await dialog.innerText()).includes('执行方案还需要单独确认'));
  const authorize=dialog.getByRole('button',{name:'授权下载这些资源',exact:true});
  await authorize.waitFor();await page.waitForFunction(()=>!document.querySelector('dialog [data-confirmation]').disabled);
  busy=true;
  await page.waitForFunction(()=>document.querySelector('dialog [data-confirmation]').disabled);
  busy=false;
  await page.waitForFunction(()=>!document.querySelector('dialog [data-confirmation]').disabled);
  await page.evaluate(()=>{const old=Date.now;Date.now=()=>old()+3600000;window.restoreResourceClock=()=>{Date.now=old;};});
  await page.waitForFunction(()=>document.querySelector('dialog [data-confirmation]').disabled);
  await page.evaluate(()=>window.restoreResourceClock());
  await page.waitForFunction(()=>!document.querySelector('dialog [data-confirmation]').disabled);
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:900});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:`/tmp/contribos-native-resource-preview-${width}.png`});
  }
  await authorize.click();
  await dialog.getByRole('alert').filter({hasText:'暂未保存'}).waitFor();
  assert.equal(resources.preview.consumed,false);
  await page.waitForFunction(()=>!document.querySelector('dialog [data-confirmation]').disabled);
  await authorize.click();await dialog.waitFor({state:'hidden'});
  assert.deepEqual(mutations.filter(x=>x.kind==='confirm').map(x=>x.body),[
    {preview_hash:'a'.repeat(64),nonce:'b'.repeat(64),approved:true},
    {preview_hash:'a'.repeat(64),nonce:'b'.repeat(64),approved:true},
  ]);
  await page.getByText('资源下载已授权，等待准备。',{exact:true}).waitFor();
  assert.equal(await page.getByRole('button',{name:'按此方案开始',exact:true}).count(),0);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({ok:true,previews:1,confirmationRetries:2,desktop:true,mobile:true,realDownloads:0,executions:0,errors}));
}finally{await browser.close();}
