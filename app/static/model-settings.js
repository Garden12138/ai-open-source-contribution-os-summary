import {requestJSON} from "./api.js?v=studio-v1";
import {el, action} from "./studio.js?v=studio-v1";

function field(form, name, title, type="text", value="") {
  const label = el("label",title); const input = el("input"); input.name=name; input.type=type; input.value=value;
  label.append(input); form.append(label); return input;
}
function selectField(form,name,title,choices,value="") {
  const label = el("label",title); const select = el("select"); select.name=name; select.setAttribute("aria-label",title);
  for (const [id,text] of choices) { const option=el("option",text); option.value=id; select.append(option); }
  select.value=value; label.append(select); form.append(label); return select;
}
function encode(bytes) { let text=""; for (const byte of new Uint8Array(bytes)) text+=String.fromCharCode(byte); return btoa(text).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/g,""); }

export async function sealCredential(secret, grant) {
  if (!crypto.subtle) throw new Error("此浏览器无法安全保存密钥，请使用本机地址或 HTTPS。");
  const publicKey = await crypto.subtle.importKey("jwk",grant.public_key,{name:"RSA-OAEP",hash:"SHA-256"},false,["encrypt"]);
  const aes = await crypto.subtle.generateKey({name:"AES-GCM",length:256},true,["encrypt"]);
  const iv=crypto.getRandomValues(new Uint8Array(12));
  const aad = new TextEncoder().encode(JSON.stringify({connection_id:grant.connection_id,grant_id:grant.grant_id,key_id:grant.key_id}));
  const ciphertext=await crypto.subtle.encrypt({name:"AES-GCM",iv,additionalData:aad},aes,new TextEncoder().encode(secret));
  const wrapped=await crypto.subtle.encrypt({name:"RSA-OAEP"},publicKey,await crypto.subtle.exportKey("raw",aes));
  return {grant_id:grant.grant_id,key_id:grant.key_id,iv:encode(iv),ciphertext:encode(ciphertext),wrapped_key:encode(wrapped)};
}

export function mountModelSettings(root,options) {
  let data=null, connectionEdit=null, profileEdit=null, timer=null;
  let testJob=null, lastTestKey=null;
  const shell=el("div","","studio-model-settings");
  shell.append(el("h1","模型设置"),el("p","连接你信任的模型服务，为不同阶段选择合适的模型。"));
  const status=el("p","正在载入设置…","settings-status"); status.setAttribute("role","status"); shell.append(status);
  const content=el("div"); shell.append(content); root.replaceChildren(shell);
  async function mutate(url,payload,method="POST",key=crypto.randomUUID()) {
    await options.ensureAccess();
    return requestJSON(url,{method,headers:options.headers({"Content-Type":"application/json","Idempotency-Key":key}),body:JSON.stringify(payload)});
  }
  async function run(button,fn) {
    button.disabled=true; status.textContent="正在保存…";
    try { await fn(); status.textContent="已保存。新设置用于后续任务，正在运行的任务保留原模型。"; await load(); await options.onSaved?.(); }
    catch(error) { status.textContent=error.message; }
    finally { button.disabled=false; }
  }
  async function load() {
    try { data=await requestJSON("/api/v1/model-settings"); render(); }
    catch(error) { status.textContent=error.message; content.replaceChildren(action("重新载入",load)); }
  }
  function section(title) { const s=el("section","","settings-section"); s.append(el("h2",title)); content.append(s); return s; }
  function render() {
    content.replaceChildren();
    const minimaxPreset=(data.presets || []).find(item=>item.id==="minimax-m3-cn");
    if(minimaxPreset) {
      const preset=section("推荐默认模型");
      preset.append(el("p",`${minimaxPreset.name} · ${minimaxPreset.model} · 分析、规划、实现和审查统一使用`,"muted"));
      const quick=el("form");preset.append(quick);
      const presetSecret=field(quick,"secret","MiniMax API Key","password");presetSecret.autocomplete="new-password";
      presetSecret.placeholder="仅加密发送至独立模型网关";presetSecret.required=true;
      presetSecret.disabled=!data.credential_management_available;
      const activate=action("配置并设为四阶段默认",()=>{});activate.type="submit";activate.disabled=!data.credential_management_available;
      quick.append(activate);
      quick.addEventListener("submit",event=>{event.preventDefault();run(activate,async()=>{
        const scopeId=`minimax-cn-${crypto.randomUUID()}`;
        const grant=await mutate("/api/v1/model-connections/credential-grants",{connection_id:scopeId});
        const envelope=await sealCredential(presetSecret.value.trim(),grant);presetSecret.value="";
        const credential=await mutate("/api/v1/model-connections/credentials",{connection_id:scopeId,envelope});
        const connection=await mutate("/api/v1/model-connections",{scope_id:scopeId,expected_hash:null,connection:{
          name:minimaxPreset.name,provider:minimaxPreset.provider,base_url:minimaxPreset.base_url,credential_ref:credential.credential_ref}});
        const profile=await mutate("/api/v1/model-profiles",{scope_id:`minimax-m3-${crypto.randomUUID()}`,expected_hash:null,profile:{
          name:minimaxPreset.name,connection_id:connection.id,model:minimaxPreset.model,...minimaxPreset.profile}});
        await mutate("/api/v1/model-settings",{expected_hash:data.version?.record_hash || null,settings:{
          default:profile.id,analysis:null,planning:null,implementation:null,review:null}},"PUT");
      });});
    }
    const connections=section("模型连接");
    if (!data.credential_management_available) connections.append(el("p","密钥管理尚未连接。请按部署文档配置 MODEL_GATEWAY_MANAGEMENT_KEY，现有模型调用不受影响。","muted"));
    for (const item of data.connections) {
      const row=el("div","","settings-saved-item"), copy=el("div",item.payload.name);
      copy.append(el("small",`${item.payload.base_url} · ${item.payload.credential_ref ? "密钥已配置" : "未配置密钥"}`));
      row.append(copy,action("编辑",()=>{connectionEdit=item; render();})); connections.append(row);
    }
    const form=el("form"); connections.append(form);
    const c=connectionEdit?.payload || {};
    const scopeId=connectionEdit?.scope.split(":").slice(1).join(":") || crypto.randomUUID();
    const name=field(form,"name","连接名称","text",c.name || ""); name.required=true; name.placeholder="例如：我的模型服务";
    const provider=selectField(form,"provider","接口类型",[["minimax","MiniMax 官方（国内）"],["openai_compatible","OpenAI 兼容 API"],["nvidia_nim","NVIDIA NIM"]],c.provider || "openai_compatible");
    const url=field(form,"base_url","Base URL","url",c.base_url || ""); url.required=true; url.placeholder="https://api.example.com/v1";
    const syncEndpoint=()=>{
      if(provider.value==="nvidia_nim") url.value="https://integrate.api.nvidia.com/v1";
      if(provider.value==="minimax") url.value="https://api.minimax.cn/v1";
      url.readOnly=["nvidia_nim","minimax"].includes(provider.value);
    };
    provider.addEventListener("change",syncEndpoint);syncEndpoint();
    const secret=field(form,"secret","API Key","password"); secret.autocomplete="new-password";
    secret.placeholder=c.credential_ref ? "已保存；留空保持原密钥" : "仅发送至独立模型网关";
    secret.disabled=!data.credential_management_available;
    const buttons=el("div","","settings-actions settings-wide");
    const save=action(connectionEdit ? "保存连接新版本" : "添加连接",()=>{}); save.type="submit";
    buttons.append(save); if(connectionEdit) buttons.append(action("取消编辑",()=>{connectionEdit=null;render();})); form.append(buttons);
    let pendingEnvelope=null;
    secret.addEventListener("input",()=>{pendingEnvelope=null;});
    form.addEventListener("submit",event=>{event.preventDefault(); run(save,async()=>{
      let reference=c.credential_ref || null;
      if(secret.value || pendingEnvelope) {
        if(!pendingEnvelope) { const grant=await mutate("/api/v1/model-connections/credential-grants",{connection_id:scopeId}); pendingEnvelope=await sealCredential(secret.value.trim(),grant); secret.value=""; }
        const result=await mutate("/api/v1/model-connections/credentials",{connection_id:scopeId,envelope:pendingEnvelope}); reference=result.credential_ref;
      }
      if(!reference) throw new Error("请先填写并保存 API Key。");
      await mutate("/api/v1/model-connections",{scope_id:scopeId,expected_hash:connectionEdit?.record_hash || null,
        connection:{name:name.value,provider:provider.value,base_url:url.value,credential_ref:reference}});
      connectionEdit=null;
    });});

    const profiles=section("可用模型");
    if(!data.connections.length) { profiles.append(el("p","先添加连接，再填写模型 ID。","muted")); return; }
    for(const item of data.profiles) {
      const row=el("div","","settings-saved-item"), copy=el("div",item.payload.name);
      copy.append(el("small",`${item.payload.model} · 输出上限 ${item.payload.max_tokens} Tokens`));
      const actions=el("div","","settings-actions");
      actions.append(action("编辑",()=>{profileEdit=item;render();}),action("测试连接",()=>probe(item,"test")),action("读取模型列表",()=>probe(item,"models")));
      row.append(copy,actions); profiles.append(row);
    }
    const testStatus=el("p","","settings-status"); testStatus.id="model-test-status"; testStatus.setAttribute("role","status"); profiles.append(testStatus);
    if(testJob) renderTest();
    const pf=el("form"); profiles.append(pf); const p=profileEdit?.payload || {};
    field(pf,"name","模型显示名称","text",p.name || "").required=true;
    const connectionChoices=data.connections.map(v=>[v.id,v.payload.name]);
    if(p.connection_id && !connectionChoices.some(v=>v[0]===p.connection_id)) connectionChoices.push([p.connection_id,"此前绑定的连接版本"]);
    selectField(pf,"connection_id","使用连接",connectionChoices,p.connection_id || connectionChoices[0][0]);
    const model=field(pf,"model","模型 ID","text",p.model || ""); model.required=true; model.placeholder="服务商提供的完整模型 ID"; model.setAttribute("list","studio-model-catalog");
    const catalog=el("datalist");catalog.id="studio-model-catalog"; for(const id of testJob?.result_data?.models || []) catalog.append(Object.assign(el("option"),{value:id}));pf.append(catalog);
    const structured=selectField(pf,"structured_output","结构化响应",[["tools","工具调用（默认）"],["json_schema","JSON Schema"]],p.structured_output || "tools");
    const advanced=el("details");advanced.append(el("summary","高级参数"));const af=el("div");advanced.append(af);pf.append(advanced);
    const tokens=field(af,"max_tokens","输出 Token 上限","number",p.max_tokens || 8192);tokens.min=256;tokens.max=16384;
    const timeout=field(af,"timeout_seconds","单次调用超时（秒）","number",p.timeout_seconds || 180);timeout.min=10;timeout.max=300;
    const temperature=field(af,"temperature","Temperature（留空由服务商决定）","number",p.temperature ?? "");temperature.min=0;temperature.max=1;temperature.step=.1;
    const reasoning=selectField(af,"reasoning_effort","推理强度（模型支持时）",[["","默认"],["low","低"],["medium","中"],["high","高"]],p.reasoning_effort || "");
    const syncCapabilities=()=>{
      const selected=data.connections.find(c=>c.id===pf.elements.connection_id.value)?.payload.provider;
      structured.disabled=["nvidia_nim","minimax"].includes(selected);if(structured.disabled)structured.value="tools";
      reasoning.disabled=selected==="minimax";if(reasoning.disabled)reasoning.value="";
      if(selected==="minimax" && !model.value) model.value="MiniMax-M3";
    };
    pf.elements.connection_id.addEventListener("change",syncCapabilities);syncCapabilities();
    const psave=action(profileEdit ? "保存模型新版本" : "添加模型",()=>{});psave.type="submit";
    const pa=el("div","","settings-actions settings-wide");pa.append(psave);if(profileEdit)pa.append(action("取消编辑",()=>{profileEdit=null;render();}));pf.append(pa);
    pf.addEventListener("submit",event=>{event.preventDefault();run(psave,async()=>{
      const values=Object.fromEntries(new FormData(pf));values.max_tokens=Number(values.max_tokens);values.timeout_seconds=Number(values.timeout_seconds);
      values.temperature=values.temperature==="" ? null : Number(values.temperature);values.reasoning_effort=values.reasoning_effort || null;
      const saved=await mutate("/api/v1/model-profiles",{scope_id:profileEdit?.scope.slice(8) || crypto.randomUUID(),expected_hash:profileEdit?.record_hash || null,profile:values});
      if(!data.version) await mutate("/api/v1/model-settings",{expected_hash:null,settings:{default:saved.id}},"PUT");
      profileEdit=null;
    });});
    const defaults=section("默认模型与阶段覆盖"); const df=el("form");defaults.append(df);
    const current=data.version?.payload || {};
    const choices=data.profiles.map(v=>[v.id,v.payload.name]);
    for(const identifier of Object.values(current)) if(identifier && !choices.some(v=>v[0]===identifier)) choices.push([identifier,"已绑定的历史模型版本"]);
    for(const [stage,title] of [["default","默认模型"],["analysis","机会分析"],["planning","方案讨论"],["implementation","代码实现"],["review","独立审查"]]) {
      selectField(df,stage,title,[["",stage==="default" ? "未选择" : "继承默认模型"],...choices],current[stage] || "");
    }
    const dsave=action("保存默认设置",()=>{});dsave.type="submit";df.append(dsave);
    df.addEventListener("submit",event=>{event.preventDefault();run(dsave,()=>mutate("/api/v1/model-settings",{expected_hash:data.version?.record_hash || null,
      settings:Object.fromEntries([...new FormData(df)].map(([key,value])=>[key,value || null]))},"PUT"));});
  }
  async function probe(profile,operation) {
    if(testJob && !["succeeded","failed","cancelled","timed_out"].includes(testJob.state)) { status.textContent="请等待或取消当前连接测试。"; return; }
    try { lastTestKey=crypto.randomUUID();testJob=await mutate("/api/v1/model-connections/test-jobs",{profile_id:profile.id,operation},"POST",lastTestKey); renderTest();poll(); }
    catch(error) { status.textContent=error.message; }
  }
  function renderTest() {
    const target=document.querySelector("#model-test-status");if(!target || !testJob)return;
    const done=["succeeded","failed","cancelled","timed_out"].includes(testJob.state);
    target.replaceChildren(el("span",testJob.state==="succeeded" ? (testJob.result_data.models ? `已读取 ${testJob.result_data.models.length} 个模型。` : `连接与结构化响应正常 · ${testJob.result_data.duration_ms} ms · 用量未知`) : done ? testJob.error_message || "测试已取消或超时" : "连接测试进行中…"));
    if(!done)target.append(action("取消",async()=>{try{await mutate(`/api/v1/jobs/${testJob.id}/cancel`,{});poll();}catch(e){status.textContent=e.message;}}));
  }
  async function poll() {
    clearTimeout(timer);
    try { testJob=await requestJSON(`/api/v1/jobs/${testJob.id}`);renderTest();if(testJob.state==="succeeded" && testJob.result_data.models)render(); }
    catch(error) { status.textContent=error.message; }
    if(testJob && !["succeeded","failed","cancelled","timed_out"].includes(testJob.state))timer=setTimeout(poll,2000);
  }
  load().then(()=>{if(data)status.textContent="密钥保存后不回显；只有测试或启动任务时才会调用模型。";});
}
